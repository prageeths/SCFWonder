"""Transaction Agent tools: program lookup + invoice pricing."""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..config import BASE_RATE
from ._common import log_event


class FindProgramArgs(BaseModel):
    buyer_id: int
    seller_id: int
    product: str


class PriceInvoiceArgs(BaseModel):
    invoice_id: int


def tool_find_program(
    db: Session, *, buyer_id: int, seller_id: int, product: str
) -> dict:
    prog = db.query(models.Program).filter(
        models.Program.buyer_id == buyer_id,
        models.Program.seller_id == seller_id,
        models.Program.product == product,
        models.Program.status == "ACTIVE",
    ).one_or_none()
    if prog is None:
        return {"found": False}
    return {
        "found": True,
        "program_id": prog.id,
        "name": prog.name,
        "credit_limit_usd": prog.credit_limit_usd,
        "utilised_usd": prog.utilised_usd,
        "headroom_usd": round(prog.credit_limit_usd - prog.utilised_usd, 2),
        "spread_override": prog.spread_override,
    }


def tool_price_invoice(db: Session, *, invoice_id: int) -> dict:
    """Compute the all-in fee for an invoice using the platform base rate plus
    the blended buyer/seller credit spread (or program override). Writes the
    base_rate, credit_spread, fee_usd and funded_amount_usd back onto the
    invoice row."""
    inv = db.query(models.Invoice).get(invoice_id)
    if inv is None:
        return {"ok": False, "error": f"Unknown invoice {invoice_id}"}

    program = db.query(models.Program).get(inv.program_id) if inv.program_id else None
    if program is not None and program.spread_override is not None:
        spread = program.spread_override
    else:
        b_sp = inv.buyer.risk_profile.credit_spread if inv.buyer.risk_profile else 0.02
        s_sp = inv.seller.risk_profile.credit_spread if inv.seller.risk_profile else 0.02
        spread = round((b_sp + s_sp) / 2.0, 4)

    period = (inv.tenor_days + (inv.grace_period_days or 0)) / 360.0
    fee = round(inv.amount_usd * (BASE_RATE + spread) * period, 2)
    funded = round(inv.amount_usd - fee, 2)

    inv.base_rate = BASE_RATE
    inv.credit_spread = spread
    inv.fee_usd = fee
    inv.funded_amount_usd = funded
    db.flush()

    log_event(
        db, agent="transaction_agent", action="INVOICE_PRICED",
        node="transaction_agent",
        message=(
            f"base_rate={BASE_RATE:.2%} + spread={spread:.2%} × period={period:.3f} "
            f"× ${inv.amount_usd:,.2f} → fee=${fee:,.2f}, funded=${funded:,.2f}"
        ),
        invoice_id=inv.id,
        payload={"base_rate": BASE_RATE, "spread": spread, "fee_usd": fee, "funded_usd": funded},
    )
    return {
        "ok": True, "base_rate": BASE_RATE, "credit_spread": spread,
        "fee_usd": fee, "funded_amount_usd": funded,
    }


def build_transaction_tools(db: Session):
    TOOL_find_program = StructuredTool.from_function(
        name="find_program",
        description="Look up the active Program for (buyer, seller, product).",
        args_schema=FindProgramArgs,
        func=lambda buyer_id, seller_id, product: tool_find_program(
            db, buyer_id=buyer_id, seller_id=seller_id, product=product
        ),
    )
    TOOL_price_invoice = StructuredTool.from_function(
        name="price_invoice",
        description=(
            "Compute the all-in fee for an invoice: amount × (base_rate + credit_spread) "
            "× (tenor + grace) / 360. Writes fee and funded_amount onto the invoice row."
        ),
        args_schema=PriceInvoiceArgs,
        func=lambda invoice_id: tool_price_invoice(db, invoice_id=invoice_id),
    )
    return TOOL_find_program, TOOL_price_invoice


TOOL_find_program = None
TOOL_price_invoice = None
