"""Review Agent tool: decides on program-limit overages."""
from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..config import RATING_LADDER
from ._common import log_event


class DecideOverageArgs(BaseModel):
    invoice_id: int
    overage_usd: float = Field(..., gt=0)


def tool_decide_overage(db: Session, *, invoice_id: int, overage_usd: float) -> dict:
    """Apply the Review Agent policy:

    * Both parties rated BBB or better  → approve temporary increase.
    * OR overage ≤ 15% of program limit → approve.
    * Otherwise                         → deny.
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

    if ratings_ok or within_tolerance:
        reason = (
            f"Approved temporary increase of ${overage_usd:,.0f}: "
            f"ratings_ok={ratings_ok}, within_15pct={within_tolerance}"
        )
        if program is not None:
            program.credit_limit_usd = round(program.credit_limit_usd + overage_usd, 2)
        log_event(
            db, agent="review_agent", action="TEMP_INCREASE_APPROVED",
            node="review_agent", severity="DECISION", message=reason,
            invoice_id=inv.id, program_id=program.id if program else None,
        )
        return {"decision": "TEMP_INCREASE", "reason": reason}

    reason = (
        f"Denied temporary increase: overage ${overage_usd:,.0f} > 15% of program "
        f"limit (${threshold:,.0f}) and ratings below BBB."
    )
    inv.status = "REJECTED"
    inv.decision_reason = reason
    log_event(
        db, agent="review_agent", action="TEMP_INCREASE_DENIED",
        node="review_agent", severity="DECISION", message=reason,
        invoice_id=inv.id, program_id=program.id if program else None,
    )
    return {"decision": "DENIED", "reason": reason}


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
