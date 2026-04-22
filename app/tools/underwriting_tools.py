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
from ..context import company_context, joint_risk_context
from ..llm import (
    RatingAnalystResult, SYSTEM_RATING_ANALYST, SYSTEM_UNDERWRITER,
    UnderwriterRecommendation, facts_block, safe_structured_call,
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


def _enforce_rating_policy(rating: str, company: models.Company) -> tuple[str, list[str]]:
    """Apply the non-negotiable floors / caps to *any* rating produced by
    either the LLM or the deterministic fallback. Returns (final, reasons)."""
    reasons: list[str] = []
    final = rating if rating in RATING_LADDER else "B"

    # --- named majors + revenue floors ---
    def _max_rev(c: models.Company) -> float:
        best = float(c.annual_revenue_usd or 0.0)
        cur = c.parent
        seen = set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            if cur.annual_revenue_usd:
                best = max(best, float(cur.annual_revenue_usd))
            cur = cur.parent
        return best

    max_rev = _max_rev(company)
    blob = " | ".join([company.name or ""] + [
        (a.name or "") for a in _ancestors_of(company)
    ]).lower()
    for tag in NAMED_MAJORS_AAA:
        if tag.lower() in blob and RATING_LADDER.index("AAA") < RATING_LADDER.index(final):
            reasons.append(f"named-major floor → AAA (via '{tag}')")
            final = "AAA"; break
    for tag in NAMED_MAJORS_AA:
        if tag.lower() in blob and RATING_LADDER.index("AA") < RATING_LADDER.index(final):
            reasons.append(f"named-major floor → AA (via '{tag}')")
            final = "AA"; break
    if max_rev >= THRESHOLD_AAA_REVENUE and RATING_LADDER.index("AAA") < RATING_LADDER.index(final):
        reasons.append(f"revenue ${max_rev/1e9:.0f}B ≥ $250B → AAA")
        final = "AAA"
    elif max_rev >= THRESHOLD_AA_REVENUE and RATING_LADDER.index("AA") < RATING_LADDER.index(final):
        reasons.append(f"revenue ${max_rev/1e9:.0f}B ≥ $100B → AA")
        final = "AA"

    own_rev = float(company.annual_revenue_usd or 0.0)
    if 0 < own_rev < THRESHOLD_B_MAX_REVENUE and final != "B":
        reasons.append(f"revenue ${own_rev/1e6:.2f}M < $5M → pinned at B")
        final = "B"

    yrs = _years_operated(company)
    if yrs is not None and yrs < 2 and RATING_LADDER.index(final) < RATING_LADDER.index("BB"):
        reasons.append(f"new entrant: {yrs}y history → cap at BB")
        final = "BB"
    elif yrs is not None and yrs < 5 and RATING_LADDER.index(final) < RATING_LADDER.index("A"):
        reasons.append(f"young firm: {yrs}y history → cap at A")
        final = "A"

    return final, reasons


def _ancestors_of(company: models.Company) -> list[models.Company]:
    chain: list[models.Company] = []
    cur = company.parent
    seen = set()
    while cur is not None and cur.id not in seen:
        seen.add(cur.id); chain.append(cur); cur = cur.parent
    return chain


def _deterministic_rating(company: models.Company) -> tuple[str, float, float]:
    """Legacy revenue/tenure/industry/country formula used as fallback."""
    rng = random.Random(company.id * 7919 + 13)
    revenue = float(company.annual_revenue_usd or rng.uniform(5e6, 5e9))
    leverage = rng.uniform(0.2, 0.7)
    size_pd = 0.030 * (1_000_000.0 / max(revenue, 100_000.0)) ** 0.45
    industry_risk = INDUSTRY_RISK.get(company.industry or "", 0.004) * 0.5
    country_risk = COUNTRY_RISK.get(company.country or "US", 0.002) * 0.5
    years = _years_operated(company)
    tenure_adj = 0.010 if years is None else max(0.0, 0.020 * math.exp(-years / 2.0))
    pd_1y = max(0.0002, min(0.20,
        size_pd + tenure_adj + industry_risk + country_risk + (leverage - 0.45) * 0.005))
    base = "CCC"
    for r, (max_pd, _) in RATING_BANDS.items():
        if pd_1y <= max_pd:
            base = r; break
    band_spread = RATING_BANDS[base][1]
    spread = max(0.0025, round(band_spread + rng.uniform(-0.0008, 0.0015), 4))
    return base, pd_1y, spread


def tool_build_risk_profile(
    db: Session, *, company_id: int, seed_mode: bool = False,
) -> dict:
    """Assign (or refresh) the credit profile for a single company.

    Behaviour:
      * When ``llm_enabled()`` is True AND ``seed_mode`` is False, the Rating
        Analyst LLM produces the rating / PD / spread / rationale from the
        full :func:`company_context`. The platform then applies the policy
        floors/caps (named majors, revenue thresholds, tenure caps) so the
        LLM cannot violate hard rules.
      * Otherwise a deterministic revenue/tenure/industry model is used
        (used during seed runs for speed and offline).
    """
    company: Optional[models.Company] = db.query(models.Company).get(company_id)
    if company is None:
        return {"ok": False, "error": f"Unknown company {company_id}"}

    deterministic_rating, deterministic_pd, deterministic_spread = _deterministic_rating(company)
    rated_by = "deterministic_fallback"
    llm_rationale: Optional[str] = None
    llm_raw: Optional[RatingAnalystResult] = None

    if llm_enabled() and not seed_mode:
        ctx = company_context(db, company)
        ctx["deterministic_model_hint"] = {
            "rating": deterministic_rating,
            "pd_1y": deterministic_pd,
            "credit_spread": deterministic_spread,
        }
        llm_raw = safe_structured_call(
            SYSTEM_RATING_ANALYST,
            (
                "Produce the rating / PD / spread for this company from its full "
                "context. Respect the policy floors and caps in the system prompt. "
                "Structured output only.\n\n"
                + facts_block(ctx)
            ),
            RatingAnalystResult,
            label="rating_analyst",
        )

    if llm_raw is not None and llm_raw.rating in RATING_LADDER:
        proposed_rating = llm_raw.rating
        proposed_pd = max(0.0001, min(0.20, float(llm_raw.pd_1y)))
        proposed_spread = max(0.0025, min(0.06, float(llm_raw.credit_spread)))
        llm_rationale = llm_raw.rationale
        rated_by = "llm_rating_analyst"
    else:
        proposed_rating = deterministic_rating
        proposed_pd = deterministic_pd
        proposed_spread = deterministic_spread

    # Apply non-negotiable policy. The LLM can never violate floors / caps.
    final_rating, policy_reasons = _enforce_rating_policy(proposed_rating, company)
    if final_rating != proposed_rating:
        # Pull PD/spread into line with the band median of the final rating
        # so downstream consumers see consistent numbers.
        proposed_pd = max(0.0001, RATING_BANDS[final_rating][0] * 0.6)
        proposed_spread = max(0.0025, min(0.06, RATING_BANDS[final_rating][1]))

    industry_risk = INDUSTRY_RISK.get(company.industry or "", 0.004)
    country_risk = COUNTRY_RISK.get(company.country or "US", 0.002)
    years_eff = _years_operated(company)

    policy_line = "; ".join(policy_reasons) if policy_reasons else "none triggered"
    notes_parts = [
        f"Rated by {rated_by}: rating={final_rating}, "
        f"PD_1y={proposed_pd*100:.2f}%, spread={proposed_spread*100:.2f}%.",
        f"Policy floors/caps applied: {policy_line}.",
    ]
    if llm_rationale:
        notes_parts.append(f"LLM rationale: {llm_rationale}")
    notes = " ".join(notes_parts)

    existing = db.query(models.RiskProfile).filter(
        models.RiskProfile.company_id == company.id
    ).one_or_none()
    if existing:
        existing.rating = final_rating
        existing.pd_1y = proposed_pd
        existing.credit_spread = proposed_spread
        existing.industry_risk = industry_risk
        existing.country_risk = country_risk
        existing.tenure_years = years_eff or 0
        existing.notes = notes
        existing.last_reviewed = _dt.datetime.utcnow()
    else:
        existing = models.RiskProfile(
            company_id=company.id,
            rating=final_rating,
            pd_1y=proposed_pd,
            credit_spread=proposed_spread,
            industry_risk=industry_risk,
            country_risk=country_risk,
            tenure_years=years_eff or 0,
            notes=notes,
        )
        db.add(existing)
    db.flush()

    log_event(
        db, agent="underwriter_agent", action="RISK_PROFILED",
        node="underwriter_agent", severity="DECISION",
        message=(
            f"{company.name}: rating={final_rating}, spread={proposed_spread:.2%}, "
            f"PD_1y={proposed_pd:.2%} (rated by {rated_by})."
        ),
        company_id=company.id,
        payload={
            "rating": final_rating, "pd_1y": proposed_pd,
            "credit_spread": proposed_spread,
            "rated_by": rated_by,
            "llm_rationale": llm_rationale,
            "policy_reasons": policy_reasons,
            "deterministic_hint": {
                "rating": deterministic_rating, "pd_1y": deterministic_pd,
                "credit_spread": deterministic_spread,
            },
        },
    )
    return {
        "ok": True, "company_id": company.id,
        "rating": final_rating, "pd_1y": proposed_pd,
        "credit_spread": proposed_spread,
        "rated_by": rated_by,
        "llm_rationale": llm_rationale,
        "policy_reasons": policy_reasons,
    }


def _clamp_program_limit(raw_usd: float) -> tuple[float, bool]:
    """Apply the hard $100M program ceiling (spec §1).

    Returns (clamped_usd, was_clamped).
    """
    if raw_usd > PROGRAM_FUNDING_HARD_CEILING_USD:
        return PROGRAM_FUNDING_HARD_CEILING_USD, True
    return round(raw_usd, 2), False


# ---------------------------------------------------------------------------
# Joint risk assessment
# ---------------------------------------------------------------------------
#
# A factoring invoice carries buyer AND seller credit risk — but the weight
# shifts by product:
#
#   * FACTORING          — risk is mostly on the BUYER (they pay at maturity).
#                          Buyer weight = 70%, Seller weight = 30%.
#   * REVERSE_FACTORING  — risk is mostly on the BUYER (they owe the funder),
#                          Seller risk (dilution) is secondary.
#                          Buyer weight = 75%, Seller weight = 25%.
#
# The combined score is a PD (probability of default) blended by weight; the
# combined rating is the *worse* of the two parties (the weakest link) so
# that a BB seller can't be "averaged away" by an AAA buyer.
# ---------------------------------------------------------------------------

_PRODUCT_WEIGHTS = {
    "FACTORING": {"buyer": 0.70, "seller": 0.30},
    "REVERSE_FACTORING": {"buyer": 0.75, "seller": 0.25},
}


def _joint_risk_assessment(
    buyer_rp: models.RiskProfile,
    seller_rp: models.RiskProfile,
    product: str,
) -> dict:
    """Return a product-aware joint risk view of both counterparties."""
    weights = _PRODUCT_WEIGHTS.get(product, {"buyer": 0.5, "seller": 0.5})

    b_idx = RATING_LADDER.index(buyer_rp.rating)
    s_idx = RATING_LADDER.index(seller_rp.rating)
    combined_rating = RATING_LADDER[max(b_idx, s_idx)]  # weaker = higher idx

    # Blended 1y PD. Both are decimal.
    blended_pd = weights["buyer"] * buyer_rp.pd_1y + weights["seller"] * seller_rp.pd_1y

    # Blended spread — informational only (actual pricing still uses the mean
    # of the two spreads for BAU consistency with SCF Marvel).
    blended_spread = (
        weights["buyer"] * buyer_rp.credit_spread
        + weights["seller"] * seller_rp.credit_spread
    )

    cutoff_buyer_rating = "BBB"
    cutoff_seller_rating = "BB"
    buyer_ok = b_idx <= RATING_LADDER.index(cutoff_buyer_rating)
    seller_ok = s_idx <= RATING_LADDER.index(cutoff_seller_rating)

    return {
        "product": product,
        "buyer_weight": weights["buyer"],
        "seller_weight": weights["seller"],
        "buyer_rating": buyer_rp.rating,
        "seller_rating": seller_rp.rating,
        "combined_rating": combined_rating,
        "buyer_pd_1y": round(buyer_rp.pd_1y, 4),
        "seller_pd_1y": round(seller_rp.pd_1y, 4),
        "blended_pd_1y": round(blended_pd, 4),
        "buyer_spread": round(buyer_rp.credit_spread, 4),
        "seller_spread": round(seller_rp.credit_spread, 4),
        "blended_spread": round(blended_spread, 4),
        "buyer_ok": buyer_ok,
        "seller_ok": seller_ok,
        "ratings_ok": (buyer_ok and seller_ok),
        "cutoff_buyer_rating": cutoff_buyer_rating,
        "cutoff_seller_rating": cutoff_seller_rating,
    }


def _format_rejection_reason(
    invoice: models.Invoice, joint: dict,
) -> str:
    """Human-readable, numbered rationale for an underwriter decline."""
    buyer = invoice.buyer
    seller = invoice.seller

    lines = [
        "Underwriter declined — joint risk assessment failed.",
        f"• Invoice: ${invoice.amount_usd:,.2f} USD "
        f"({invoice.amount:,.2f} {invoice.currency}), {invoice.product}, "
        f"tenor {invoice.tenor_days}d.",
        (
            f"• Buyer {buyer.name}: rating {joint['buyer_rating']} "
            f"(PD 1y {joint['buyer_pd_1y']*100:.2f}%); "
            f"required ≤ {joint['cutoff_buyer_rating']} — "
            f"{'OK' if joint['buyer_ok'] else 'FAIL'}."
        ),
        (
            f"• Seller {seller.name}: rating {joint['seller_rating']} "
            f"(PD 1y {joint['seller_pd_1y']*100:.2f}%); "
            f"required ≤ {joint['cutoff_seller_rating']} — "
            f"{'OK' if joint['seller_ok'] else 'FAIL'}."
        ),
        (
            f"• Joint view for {joint['product']}: buyer weight "
            f"{joint['buyer_weight']*100:.0f}%, seller weight "
            f"{joint['seller_weight']*100:.0f}% → "
            f"combined rating {joint['combined_rating']} "
            f"(weakest of the two), blended PD "
            f"{joint['blended_pd_1y']*100:.2f}%."
        ),
    ]
    failing = []
    if not joint["buyer_ok"]:
        failing.append(f"buyer must be ≤ {joint['cutoff_buyer_rating']}")
    if not joint["seller_ok"]:
        failing.append(f"seller must be ≤ {joint['cutoff_seller_rating']}")
    lines.append("• Binding cutoff: " + " AND ".join(failing) + ".")
    return "\n".join(lines)


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

    joint = _joint_risk_assessment(b_rp, s_rp, invoice.product)
    ratings_ok = joint["ratings_ok"]

    raw_program_limit = max(requested_limit_usd * 4.0, invoice.amount_usd * 5.0)
    fallback_limit, _ = _clamp_program_limit(raw_program_limit)

    # --- LLM-driven sizing with full both-sides context ---
    #
    # We always call the LLM when enabled. The LLM returns APPROVE/DECLINE +
    # a prudent limit recommendation ≤ $100M, informed by both parties'
    # credit profiles, existing program books, and payment histories.
    from ..tools.credit_limit_tools import tool_hierarchical_headroom
    buyer_hr = tool_hierarchical_headroom(db, company_id=buyer.id, product=invoice.product)
    seller_hr = tool_hierarchical_headroom(db, company_id=seller.id, product=invoice.product)
    buyer_head = buyer_hr.get("headroom_usd", 0.0)
    seller_head = seller_hr.get("headroom_usd", 0.0)

    llm_rec: Optional[UnderwriterRecommendation] = None
    if llm_enabled():
        ctx = joint_risk_context(
            db, invoice,
            buyer_head=buyer_head, seller_head=seller_head,
            buyer_breakdown=buyer_hr.get("breakdown", {}),
            seller_breakdown=seller_hr.get("breakdown", {}),
            requested_limit_usd=raw_program_limit,
        )
        ctx["joint_risk_assessment"] = joint
        ctx["deterministic_hint"] = {
            "rule_engine_decision": "APPROVE" if ratings_ok else "DECLINE",
            "rule_engine_suggested_limit_usd": fallback_limit,
        }
        human = (
            "Decide APPROVE or DECLINE for this new bilateral program and, "
            "if APPROVE, pick a prudent program limit (USD) ≤ "
            f"${PROGRAM_FUNDING_HARD_CEILING_USD:,.0f}. Weigh BOTH parties' "
            "credit profiles, existing programs and payment histories.\n\n"
            + facts_block(ctx)
        )
        llm_rec = safe_structured_call(
            SYSTEM_UNDERWRITER, human, UnderwriterRecommendation,
            label="underwriter_decide_new_program",
        )

    # Deterministic ratings-based veto always wins.
    if not ratings_ok:
        reason = _format_rejection_reason(invoice, joint)
        if llm_rec is not None:
            reason += f"\n• Underwriter LLM memo: {llm_rec.rationale}"
        invoice.status = "REJECTED"
        invoice.decision_reason = reason
        log_event(
            db, agent="underwriter_agent", action="PROGRAM_DECLINED",
            node="underwriter_agent", severity="DECISION",
            message=reason, invoice_id=invoice.id,
            payload={"joint_risk_assessment": joint,
                     "llm_decision": llm_rec.decision if llm_rec else None,
                     "llm_rationale": llm_rec.rationale if llm_rec else None},
        )
        return {"approved": False, "reason": reason,
                "joint_risk_assessment": joint,
                "llm_rationale": llm_rec.rationale if llm_rec else None}

    # LLM owns the sizing decision when enabled; guardrails then clamp.
    sized_by = "rule_engine"
    if llm_rec is not None and llm_rec.decision == "APPROVE":
        proposed_limit = max(0.0, float(llm_rec.recommended_program_limit_usd))
        sized_by = "llm_underwriter"
    else:
        proposed_limit = fallback_limit

    # Hard clamps (in order): platform ceiling, buyer subtree, seller subtree.
    pre_clamp = proposed_limit
    proposed_limit = min(proposed_limit, PROGRAM_FUNDING_HARD_CEILING_USD)
    proposed_limit = min(proposed_limit, buyer_head, seller_head)
    # Never go below the invoice itself — otherwise we couldn't fund this invoice.
    if proposed_limit < invoice.amount_usd:
        proposed_limit = min(
            invoice.amount_usd,
            PROGRAM_FUNDING_HARD_CEILING_USD,
            max(buyer_head, 0.0), max(seller_head, 0.0),
        )
    proposed_limit = round(proposed_limit, 2)
    clamped = (pre_clamp > proposed_limit)

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

    joint_line = (
        f"Joint risk for {joint['product']}: buyer {joint['buyer_rating']} "
        f"({joint['buyer_weight']*100:.0f}% weight), "
        f"seller {joint['seller_rating']} "
        f"({joint['seller_weight']*100:.0f}% weight) → "
        f"combined {joint['combined_rating']}, "
        f"blended PD {joint['blended_pd_1y']*100:.2f}%."
    )
    msg_lines = [
        (
            f"Approved new program {program.id}: ${program.credit_limit_usd:,.0f} "
            f"bilateral limit for {program.name}."
        ),
        f"• {joint_line}",
    ]
    if clamped:
        msg_lines.append(
            f"• Limit clamped from ${raw_program_limit:,.0f} to the "
            f"${PROGRAM_FUNDING_HARD_CEILING_USD:,.0f} platform ceiling."
        )
    if llm_rec is not None:
        msg_lines.append(f"• Underwriter LLM memo: {llm_rec.rationale}")
    msg = "\n".join(msg_lines)

    log_event(
        db, agent="underwriter_agent", action="PROGRAM_APPROVED",
        node="underwriter_agent", severity="DECISION",
        message=msg,
        invoice_id=invoice.id, program_id=program.id,
        payload={
            "raw_limit_usd": raw_program_limit,
            "pre_clamp_limit_usd": pre_clamp,
            "final_limit_usd": program.credit_limit_usd,
            "clamped": clamped,
            "sized_by": sized_by,
            "joint_risk_assessment": joint,
            "llm_decision": llm_rec.decision if llm_rec else None,
            "llm_rationale": llm_rec.rationale if llm_rec else None,
        },
    )
    return {
        "approved": True,
        "program_id": program.id,
        "program_limit_usd": program.credit_limit_usd,
        "clamped_to_ceiling": clamped,
        "sized_by": sized_by,
        "joint_risk_assessment": joint,
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
