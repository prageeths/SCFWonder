"""Underwriter Agent tools: risk profiling and new-program underwriting."""
from __future__ import annotations

import datetime as _dt
import math
import random
from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..config import (
    COUNTRY_RISK, INDUSTRY_RISK, NAMED_MAJORS_AA, NAMED_MAJORS_AAA,
    PROGRAM_FUNDING_HARD_CEILING_USD, RATING_BANDS, RATING_LADDER,
    THRESHOLD_AA_REVENUE, THRESHOLD_AAA_REVENUE, THRESHOLD_B_MAX_REVENUE,
    llm_enabled,
)
from ..llm import (
    SYSTEM_UNDERWRITER, UnderwriterRecommendation,
    facts_block, safe_structured_call,
)
from ._common import log_event


# ---------------- Schemas ----------------

class RiskProfileArgs(BaseModel):
    company_id: int = Field(..., description="Primary key of the company to profile")


class DecideProgramArgs(BaseModel):
    invoice_id: int
    requested_limit_usd: float = Field(..., gt=0)


# ---------------- Risk model ----------------

def _years_operated(company: models.Company) -> Optional[int]:
    if not company.founded_year:
        return None
    return max(0, _dt.datetime.utcnow().year - int(company.founded_year))


def _max_revenue_in_tree(company: models.Company) -> float:
    best = float(company.annual_revenue_usd or 0.0)
    cursor = company.parent
    seen = set()
    while cursor is not None and cursor.id not in seen:
        seen.add(cursor.id)
        if cursor.annual_revenue_usd:
            best = max(best, float(cursor.annual_revenue_usd))
        cursor = cursor.parent
    return best


def _named_major_band(company: models.Company) -> Optional[str]:
    names = [company.name or ""]
    cursor = company.parent
    seen = set()
    while cursor is not None and cursor.id not in seen:
        seen.add(cursor.id)
        names.append(cursor.name or "")
        cursor = cursor.parent
    blob = " | ".join(names).lower()
    for tag in NAMED_MAJORS_AAA:
        if tag.lower() in blob:
            return "AAA"
    for tag in NAMED_MAJORS_AA:
        if tag.lower() in blob:
            return "AA"
    return None


def _apply_policy(base_rating: str, company: models.Company):
    reasons = []
    final = base_rating

    named = _named_major_band(company)
    if named is not None and RATING_LADDER.index(named) < RATING_LADDER.index(final):
        reasons.append(f"named-major floor → {named}")
        final = named

    max_rev = _max_revenue_in_tree(company)
    if max_rev >= THRESHOLD_AAA_REVENUE and RATING_LADDER.index("AAA") < RATING_LADDER.index(final):
        reasons.append(f"revenue ${max_rev/1e9:.0f}B ≥ $250B → AAA")
        final = "AAA"
    elif max_rev >= THRESHOLD_AA_REVENUE and RATING_LADDER.index("AA") < RATING_LADDER.index(final):
        reasons.append(f"revenue ${max_rev/1e9:.0f}B ≥ $100B → AA")
        final = "AA"

    own_rev = float(company.annual_revenue_usd or 0.0)
    if own_rev > 0 and own_rev < THRESHOLD_B_MAX_REVENUE and final != "B":
        reasons.append(f"revenue ${own_rev/1e6:.2f}M < $5M → pinned at B")
        final = "B"

    years = _years_operated(company)
    if years is not None and years < 2 and RATING_LADDER.index(final) < RATING_LADDER.index("BB"):
        reasons.append(f"new entrant: {years}y history → cap at BB")
        final = "BB"
    elif years is not None and years < 5 and RATING_LADDER.index(final) < RATING_LADDER.index("A"):
        reasons.append(f"young firm: {years}y history → cap at A")
        final = "A"

    return final, ("; ".join(reasons) if reasons else "model-derived"), years, max_rev


def tool_build_risk_profile(db: Session, *, company_id: int) -> dict:
    """Compute (or refresh) the risk profile for a company. Idempotent."""
    company: Optional[models.Company] = db.query(models.Company).get(company_id)
    if company is None:
        return {"ok": False, "error": f"Unknown company {company_id}"}

    rng = random.Random(company.id * 7919 + 13)
    revenue = float(company.annual_revenue_usd or rng.uniform(5e6, 5e9))
    leverage = rng.uniform(0.2, 0.7)

    # Revenue-driven PD. Steeper at the big end so large firms land IG.
    size_pd = 0.030 * (1_000_000.0 / max(revenue, 100_000.0)) ** 0.45
    industry_risk = INDUSTRY_RISK.get(company.industry or "", 0.004) * 0.5
    country_risk = COUNTRY_RISK.get(company.country or "US", 0.002) * 0.5
    years = _years_operated(company)
    tenure_adj = 0.010 if years is None else max(0.0, 0.020 * math.exp(-years / 2.0))

    pd_1y = max(
        0.0002,
        min(
            0.20,
            size_pd + tenure_adj + industry_risk + country_risk
            + (leverage - 0.45) * 0.005,
        ),
    )

    # Map PD -> base rating band.
    base_rating = "CCC"
    for r, (max_pd, _spread) in RATING_BANDS.items():
        if pd_1y <= max_pd:
            base_rating = r
            break

    rating, policy_reason, years_eff, max_rev = _apply_policy(base_rating, company)
    band_spread = RATING_BANDS[rating][1]
    spread = max(0.0025, round(band_spread + rng.uniform(-0.0008, 0.0015), 4))
    final_pd = max(0.0001, RATING_BANDS[rating][0] * 0.6)

    existing = db.query(models.RiskProfile).filter(
        models.RiskProfile.company_id == company.id
    ).one_or_none()
    notes = f"Model PD={pd_1y:.2%} → base={base_rating}; final={rating} ({policy_reason})."
    if existing:
        existing.rating = rating
        existing.pd_1y = final_pd
        existing.credit_spread = spread
        existing.industry_risk = industry_risk
        existing.country_risk = country_risk
        existing.tenure_years = years_eff or 0
        existing.leverage_score = leverage
        existing.notes = notes
        existing.last_reviewed = _dt.datetime.utcnow()
    else:
        existing = models.RiskProfile(
            company_id=company.id,
            rating=rating,
            pd_1y=final_pd,
            credit_spread=spread,
            industry_risk=industry_risk,
            country_risk=country_risk,
            tenure_years=years_eff or 0,
            leverage_score=leverage,
            notes=notes,
        )
        db.add(existing)
    db.flush()

    log_event(
        db,
        agent="underwriter_agent",
        action="RISK_PROFILED",
        node="underwriter_agent",
        severity="DECISION",
        message=(
            f"{company.name}: rating={rating}, spread={spread:.2%}, PD_1y={final_pd:.2%} "
            f"({policy_reason})"
        ),
        company_id=company.id,
        payload={
            "rating": rating, "base_rating": base_rating,
            "spread": spread, "pd_1y": final_pd,
            "years_operated": years_eff, "max_revenue_in_tree": max_rev,
            "policy_reason": policy_reason,
        },
    )
    return {
        "ok": True,
        "company_id": company.id,
        "rating": rating,
        "spread": spread,
        "pd_1y": final_pd,
        "years_operated": years_eff,
        "policy_reason": policy_reason,
        "notes": notes,
    }


def _clamp_program_limit(raw_usd: float) -> tuple[float, bool]:
    """Apply the hard $100M program ceiling (spec §1).

    Returns (clamped_usd, was_clamped).
    """
    if raw_usd > PROGRAM_FUNDING_HARD_CEILING_USD:
        return PROGRAM_FUNDING_HARD_CEILING_USD, True
    return round(raw_usd, 2), False


def tool_decide_new_program(
    db: Session, *, invoice_id: int, requested_limit_usd: float
) -> dict:
    """Open an underwriting case for a brand-new buyer/seller pair and
    either approve it (creating a Program) or decline.

    Policy (deterministic source of truth — LLM *cannot* override):
      * Both parties must have a RiskProfile.
      * Both parties must be rated buyer ≤ BBB and seller ≤ BB.
      * If approved, a new Program row is created sized at
        min($100M, max(4 × requested_limit, 5 × invoice.amount_usd)).
      * If an LLM is configured, it produces a banker-style rationale that
        is stored alongside the decision — but the numbers are ours.
    """
    invoice: Optional[models.Invoice] = db.query(models.Invoice).get(invoice_id)
    if invoice is None:
        return {"approved": False, "error": f"Unknown invoice {invoice_id}"}

    buyer, seller = invoice.buyer, invoice.seller
    b_rp, s_rp = buyer.risk_profile, seller.risk_profile
    if b_rp is None or s_rp is None:
        return {"approved": False, "error": "missing risk profile(s) — profile first"}

    b_idx, s_idx = RATING_LADDER.index(b_rp.rating), RATING_LADDER.index(s_rp.rating)
    cutoff_buyer = RATING_LADDER.index("BBB")
    cutoff_seller = RATING_LADDER.index("BB")
    ratings_ok = (b_idx <= cutoff_buyer and s_idx <= cutoff_seller)

    raw_program_limit = max(requested_limit_usd * 4.0, invoice.amount_usd * 5.0)
    proposed_limit, clamped = _clamp_program_limit(raw_program_limit)

    # --- optional LLM banker-style rationale ---
    llm_rec: Optional[UnderwriterRecommendation] = None
    if llm_enabled():
        facts = {
            "invoice": {
                "amount_usd": invoice.amount_usd,
                "currency": invoice.currency,
                "product": invoice.product,
                "tenor_days": invoice.tenor_days,
            },
            "buyer": {
                "name": buyer.name, "country": buyer.country,
                "industry": buyer.industry,
                "annual_revenue_usd": buyer.annual_revenue_usd,
                "rating": b_rp.rating, "pd_1y": b_rp.pd_1y,
                "credit_spread": b_rp.credit_spread,
                "tenure_years": b_rp.tenure_years,
            },
            "seller": {
                "name": seller.name, "country": seller.country,
                "industry": seller.industry,
                "annual_revenue_usd": seller.annual_revenue_usd,
                "rating": s_rp.rating, "pd_1y": s_rp.pd_1y,
                "credit_spread": s_rp.credit_spread,
                "tenure_years": s_rp.tenure_years,
            },
            "policy_rating_cutoffs": {"buyer": "BBB", "seller": "BB"},
            "program_funding_ceiling_usd": PROGRAM_FUNDING_HARD_CEILING_USD,
            "indicative_program_limit_usd_before_ceiling": raw_program_limit,
            "deterministic_decision": "APPROVE" if ratings_ok else "DECLINE",
        }
        human = (
            "Write a banker-style memo recommending APPROVE or DECLINE for the "
            "new bilateral program described below. Respect the rating cutoffs "
            "and the $100M ceiling. If you recommend APPROVE, propose a limit "
            f"no greater than ${PROGRAM_FUNDING_HARD_CEILING_USD:,.0f}.\n\n"
            + facts_block(facts)
        )
        llm_rec = safe_structured_call(
            SYSTEM_UNDERWRITER, human, UnderwriterRecommendation,
            label="underwriter_decide_new_program",
        )

    # Deterministic ratings-based veto always wins.
    if not ratings_ok:
        reason = (
            f"Declined: buyer={b_rp.rating} (cutoff BBB), "
            f"seller={s_rp.rating} (cutoff BB)."
        )
        if llm_rec is not None:
            reason += f" LLM rationale: {llm_rec.rationale}"
        invoice.status = "REJECTED"
        invoice.decision_reason = reason
        log_event(
            db, agent="underwriter_agent", action="PROGRAM_DECLINED",
            node="underwriter_agent", severity="DECISION",
            message=reason, invoice_id=invoice.id,
            payload={"llm_decision": llm_rec.decision if llm_rec else None,
                     "llm_rationale": llm_rec.rationale if llm_rec else None},
        )
        return {"approved": False, "reason": reason,
                "llm_rationale": llm_rec.rationale if llm_rec else None}

    # Honour the LLM's numeric recommendation if it's lower than our proposal
    # (we always pick the *more conservative* value).
    if llm_rec is not None and llm_rec.decision == "APPROVE":
        llm_limit, _ = _clamp_program_limit(llm_rec.recommended_program_limit_usd)
        proposed_limit = min(proposed_limit, llm_limit) if llm_limit > 0 else proposed_limit

    program = models.Program(
        name=f"{seller.name} → {buyer.name} ({invoice.product})",
        buyer_id=buyer.id, seller_id=seller.id,
        product=invoice.product,
        credit_limit_usd=round(proposed_limit, 2),
        base_currency=invoice.currency,
        grace_period_days=invoice.grace_period_days or 5,
        status="ACTIVE",
    )
    db.add(program)
    db.flush()
    invoice.program_id = program.id

    msg = (
        f"Approved new program {program.id}: ${program.credit_limit_usd:,.0f} "
        f"bilateral limit for {program.name}"
    )
    if clamped:
        msg += f" (clamped from ${raw_program_limit:,.0f} to $100M ceiling)"
    if llm_rec is not None:
        msg += f" | LLM: {llm_rec.rationale}"

    log_event(
        db, agent="underwriter_agent", action="PROGRAM_APPROVED",
        node="underwriter_agent", severity="DECISION",
        message=msg,
        invoice_id=invoice.id, program_id=program.id,
        payload={
            "raw_limit_usd": raw_program_limit,
            "final_limit_usd": program.credit_limit_usd,
            "clamped_to_ceiling": clamped,
            "llm_decision": llm_rec.decision if llm_rec else None,
            "llm_rationale": llm_rec.rationale if llm_rec else None,
        },
    )
    return {
        "approved": True,
        "program_id": program.id,
        "program_limit_usd": program.credit_limit_usd,
        "clamped_to_ceiling": clamped,
        "llm_rationale": llm_rec.rationale if llm_rec else None,
    }


# ---------------- StructuredTool wrappers ----------------

def build_underwriting_tools(db: Session):
    TOOL_build_risk_profile = StructuredTool.from_function(
        name="build_risk_profile",
        description=(
            "Compute or refresh the credit risk profile for a company (rating, "
            "PD_1y, credit spread). Applies named-major floors, revenue thresholds, "
            "and new-entrant tenure caps."
        ),
        args_schema=RiskProfileArgs,
        func=lambda company_id: tool_build_risk_profile(db, company_id=company_id),
    )
    TOOL_decide_new_program = StructuredTool.from_function(
        name="decide_new_program",
        description=(
            "Open an underwriting case for a brand-new buyer/seller pair and either "
            "approve (creating a Program) or decline based on the parties' ratings."
        ),
        args_schema=DecideProgramArgs,
        func=lambda invoice_id, requested_limit_usd: tool_decide_new_program(
            db, invoice_id=invoice_id, requested_limit_usd=requested_limit_usd,
        ),
    )
    return TOOL_build_risk_profile, TOOL_decide_new_program


TOOL_build_risk_profile = None
TOOL_decide_new_program = None
