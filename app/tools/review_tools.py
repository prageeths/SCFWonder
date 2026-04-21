"""Review Agent tool: decides on program-limit overages."""
from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..config import (
    PROGRAM_FUNDING_HARD_CEILING_USD, RATING_LADDER, llm_enabled,
)
from ..llm import (
    SYSTEM_REVIEW, ReviewRecommendation, facts_block, safe_structured_call,
)
from ._common import log_event


class DecideOverageArgs(BaseModel):
    invoice_id: int
    overage_usd: float = Field(..., gt=0)


def tool_decide_overage(db: Session, *, invoice_id: int, overage_usd: float) -> dict:
    """Review Agent policy — deterministic rules + optional LLM rationale.

    Rules (LLM cannot override):
      * APPROVE if BOTH parties rated BBB+ OR overage ≤ 15% of program limit.
      * Even if APPROVE, the resulting program limit MUST stay at or below
        PROGRAM_FUNDING_HARD_CEILING_USD (spec §1 — $100M ceiling).
      * Otherwise DENY.
    """
    inv = db.query(models.Invoice).get(invoice_id)
    if inv is None:
        return {"ok": False, "error": f"Unknown invoice {invoice_id}"}

    program = db.query(models.Program).get(inv.program_id) if inv.program_id else None
    b_rp, s_rp = inv.buyer.risk_profile, inv.seller.risk_profile

    ratings_ok = (
        b_rp is not None and s_rp is not None
        and RATING_LADDER.index(b_rp.rating) <= RATING_LADDER.index("BBB")
        and RATING_LADDER.index(s_rp.rating) <= RATING_LADDER.index("BBB")
    )
    threshold = (program.credit_limit_usd * 0.15) if program else 0.0
    within_tolerance = overage_usd <= threshold
    would_breach_ceiling = (
        program is not None
        and (program.credit_limit_usd + overage_usd) > PROGRAM_FUNDING_HARD_CEILING_USD
    )

    # Optional LLM recommendation — stored but never overrides the numbers.
    llm_rec: Optional[ReviewRecommendation] = None
    if llm_enabled() and program is not None:
        facts = {
            "invoice_usd": inv.amount_usd,
            "program_name": program.name,
            "program_limit_usd": program.credit_limit_usd,
            "program_utilised_usd": program.utilised_usd,
            "overage_usd": overage_usd,
            "program_funding_ceiling_usd": PROGRAM_FUNDING_HARD_CEILING_USD,
            "ratings": {
                "buyer": b_rp.rating if b_rp else None,
                "seller": s_rp.rating if s_rp else None,
            },
            "within_15pct_tolerance": within_tolerance,
            "would_breach_ceiling_if_approved": would_breach_ceiling,
        }
        llm_rec = safe_structured_call(
            SYSTEM_REVIEW,
            f"Recommend TEMP_INCREASE or DENY.\n\n{facts_block(facts)}",
            ReviewRecommendation,
            label="review_decide_overage",
        )

    if would_breach_ceiling:
        proposed = program.credit_limit_usd + overage_usd if program else overage_usd
        reason = "\n".join([
            "Rejected — temporary increase would breach the platform ceiling.",
            (
                f"• Invoice: ${inv.amount_usd:,.2f} USD  |  "
                f"Program headroom: "
                f"${(program.credit_limit_usd - program.utilised_usd):,.2f}."
                if program else f"• Invoice: ${inv.amount_usd:,.2f} USD."
            ),
            f"• Overage requested: ${overage_usd:,.2f}.",
            (
                f"• Current program limit: ${program.credit_limit_usd:,.2f}; "
                f"proposed after increase: ${proposed:,.2f}."
                if program else ""
            ),
            (
                f"• Platform ceiling: ${PROGRAM_FUNDING_HARD_CEILING_USD:,.2f} — "
                f"proposed limit would be ${proposed - PROGRAM_FUNDING_HARD_CEILING_USD:,.2f} over."
            ),
        ])
        reason = "\n".join(line for line in reason.splitlines() if line.strip())
        if llm_rec is not None:
            reason += f"\n• Review LLM memo: {llm_rec.rationale}"
        inv.status = "REJECTED"
        inv.decision_reason = reason
        log_event(
            db, agent="review_agent", action="TEMP_INCREASE_DENIED_CEILING",
            node="review_agent", severity="DECISION", message=reason,
            invoice_id=inv.id, program_id=program.id if program else None,
            payload={"llm_rationale": llm_rec.rationale if llm_rec else None,
                     "proposed_new_limit_usd": proposed,
                     "platform_ceiling_usd": PROGRAM_FUNDING_HARD_CEILING_USD},
        )
        return {"decision": "DENIED", "reason": reason,
                "llm_rationale": llm_rec.rationale if llm_rec else None}

    if ratings_ok or within_tolerance:
        reason = (
            f"Approved temporary increase of ${overage_usd:,.0f}: "
            f"ratings_ok={ratings_ok}, within_15pct={within_tolerance}"
        )
        if llm_rec is not None:
            reason += f" | LLM: {llm_rec.rationale}"
        if program is not None:
            program.credit_limit_usd = round(program.credit_limit_usd + overage_usd, 2)
        log_event(
            db, agent="review_agent", action="TEMP_INCREASE_APPROVED",
            node="review_agent", severity="DECISION", message=reason,
            invoice_id=inv.id, program_id=program.id if program else None,
            payload={"llm_rationale": llm_rec.rationale if llm_rec else None},
        )
        return {"decision": "TEMP_INCREASE", "reason": reason,
                "llm_rationale": llm_rec.rationale if llm_rec else None}

    over_pct = (overage_usd / program.credit_limit_usd * 100.0) if program and program.credit_limit_usd else 0.0
    reason = "\n".join([
        "Rejected — temporary program-limit increase denied.",
        f"• Invoice: ${inv.amount_usd:,.2f} USD.",
        (
            f"• Program: '{program.name}' with ${program.credit_limit_usd:,.2f} "
            f"limit (${program.utilised_usd:,.2f} used)."
            if program else "• No active program."
        ),
        f"• Overage: ${overage_usd:,.2f} — {over_pct:.1f}% of program limit.",
        f"• Auto-approve tolerance is 15% (${threshold:,.2f}) — exceeded.",
        (
            f"• Ratings: buyer {b_rp.rating if b_rp else '—'}, "
            f"seller {s_rp.rating if s_rp else '—'} — at least one is below BBB."
        ),
    ])
    if llm_rec is not None:
        reason += f"\n• Review LLM memo: {llm_rec.rationale}"
    inv.status = "REJECTED"
    inv.decision_reason = reason
    log_event(
        db, agent="review_agent", action="TEMP_INCREASE_DENIED",
        node="review_agent", severity="DECISION", message=reason,
        invoice_id=inv.id, program_id=program.id if program else None,
        payload={
            "invoice_usd": inv.amount_usd,
            "overage_usd": overage_usd,
            "overage_pct_of_program_limit": over_pct,
            "tolerance_threshold_usd": threshold,
            "buyer_rating": b_rp.rating if b_rp else None,
            "seller_rating": s_rp.rating if s_rp else None,
            "llm_rationale": llm_rec.rationale if llm_rec else None,
        },
    )
    return {"decision": "DENIED", "reason": reason,
            "llm_rationale": llm_rec.rationale if llm_rec else None}


def build_review_tools(db: Session):
    TOOL_decide_overage = StructuredTool.from_function(
        name="decide_overage",
        description=(
            "Decide whether a one-off program-limit overage is acceptable "
            "(TEMP_INCREASE) or must be rejected (DENIED)."
        ),
        args_schema=DecideOverageArgs,
        func=lambda invoice_id, overage_usd: tool_decide_overage(
            db, invoice_id=invoice_id, overage_usd=overage_usd
        ),
    )
    return (TOOL_decide_overage,)


TOOL_decide_overage = None
