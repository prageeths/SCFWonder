"""Credit Limit Agent tools: hierarchical limit math + reservation bookkeeping."""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..config import PRODUCT_FACTORING, PRODUCT_REVERSE_FACTORING
from ._common import ancestors, descendant_ids, log_event

GLOBAL = "GLOBAL"


# ---------------- Schemas ----------------

class EnsureLimitsArgs(BaseModel):
    company_id: int
    product: str


class HeadroomArgs(BaseModel):
    company_id: int
    product: str


class ReserveLimitsArgs(BaseModel):
    invoice_id: int


# ---------------- Implementations ----------------

def _derive_global_limit(company: models.Company) -> float:
    revenue = company.annual_revenue_usd or 5e7
    return max(1_000_000.0, min(5_000_000_000.0, revenue * 0.08))


def _ensure_global(db: Session, company: models.Company) -> models.CreditLimit:
    existing = db.query(models.CreditLimit).filter(
        models.CreditLimit.company_id == company.id,
        models.CreditLimit.product == GLOBAL,
    ).one_or_none()
    if existing:
        return existing
    cl = models.CreditLimit(
        company_id=company.id, product=GLOBAL,
        limit_usd=round(_derive_global_limit(company), 2),
        utilised_usd=0.0, set_by="credit_limit_agent",
    )
    db.add(cl)
    db.flush()
    log_event(
        db, agent="credit_limit_agent", action="GLOBAL_LIMIT_SET",
        node="credit_limit_agent", severity="DECISION",
        message=f"Set global credit limit for {company.name} = ${cl.limit_usd:,.0f}",
        company_id=company.id, payload={"limit_usd": cl.limit_usd},
    )
    return cl


def _ensure_product(db: Session, company: models.Company, product: str) -> models.CreditLimit:
    if product not in (PRODUCT_FACTORING, PRODUCT_REVERSE_FACTORING):
        raise ValueError(f"Unknown product {product}")
    existing = db.query(models.CreditLimit).filter(
        models.CreditLimit.company_id == company.id,
        models.CreditLimit.product == product,
    ).one_or_none()
    if existing:
        return existing
    global_cl = _ensure_global(db, company)
    share = 0.6 if product == PRODUCT_FACTORING else 0.4
    cl = models.CreditLimit(
        company_id=company.id, product=product,
        limit_usd=round(global_cl.limit_usd * share, 2),
        utilised_usd=0.0, set_by="credit_limit_agent",
    )
    db.add(cl)
    db.flush()
    log_event(
        db, agent="credit_limit_agent", action="PRODUCT_LIMIT_SET",
        node="credit_limit_agent", severity="DECISION",
        message=f"Set {product} sub-limit for {company.name} = ${cl.limit_usd:,.0f}",
        company_id=company.id, payload={"product": product, "limit_usd": cl.limit_usd},
    )
    return cl


def tool_ensure_limits(db: Session, *, company_id: int, product: str) -> dict:
    """Make sure a company has a GLOBAL limit and the requested product sub-limit."""
    company = db.query(models.Company).get(company_id)
    if company is None:
        return {"ok": False, "error": f"Unknown company {company_id}"}
    _ensure_global(db, company)
    _ensure_product(db, company, product)
    return {"ok": True}


def _subtree_utilisation(
    db: Session, company: models.Company, product: Optional[str]
) -> float:
    ids = [company.id] + descendant_ids(db, company.id)
    q = db.query(models.CreditLimit).filter(models.CreditLimit.company_id.in_(ids))
    if product is None:
        q = q.filter(models.CreditLimit.product == GLOBAL)
    else:
        q = q.filter(models.CreditLimit.product == product)
    return sum(cl.utilised_usd for cl in q.all())


def tool_hierarchical_headroom(db: Session, *, company_id: int, product: str) -> dict:
    """Return the tightest headroom (USD) across the company AND every ancestor,
    checking both the GLOBAL limit and the product-specific limit at each level.
    Includes a per-level breakdown so the reasoning is transparent."""
    company = db.query(models.Company).get(company_id)
    if company is None:
        return {"ok": False, "error": f"Unknown company {company_id}"}

    breakdown: Dict[str, float] = {}
    worst = float("inf")

    for ancestor in ancestors(company):
        _ensure_global(db, ancestor)
        _ensure_product(db, ancestor, product)
        for kind in (product, GLOBAL):
            cl = db.query(models.CreditLimit).filter(
                models.CreditLimit.company_id == ancestor.id,
                models.CreditLimit.product == kind,
            ).one_or_none()
            if cl is None:
                continue
            used = _subtree_utilisation(db, ancestor, None if kind == GLOBAL else kind)
            headroom = max(0.0, cl.limit_usd - used)
            breakdown[f"{ancestor.name}:{kind}"] = round(headroom, 2)
            worst = min(worst, headroom)

    if worst == float("inf"):
        worst = 0.0
    return {"ok": True, "headroom_usd": round(worst, 2), "breakdown": breakdown}


def tool_reserve_limits(db: Session, *, invoice_id: int) -> dict:
    """Reserve the invoice amount against the program + both parties' GLOBAL and
    product limits. Called after an approval decision."""
    invoice = db.query(models.Invoice).get(invoice_id)
    if invoice is None:
        return {"ok": False, "error": f"Unknown invoice {invoice_id}"}

    amount = invoice.amount_usd
    product = invoice.product

    if invoice.program_id:
        program = db.query(models.Program).get(invoice.program_id)
        if program is not None:
            program.utilised_usd = round(program.utilised_usd + amount, 2)
            log_event(
                db, agent="credit_limit_agent", action="PROGRAM_UTILISATION_BUMPED",
                node="credit_limit_agent",
                message=(
                    f"{program.name}: +${amount:,.0f} "
                    f"(used ${program.utilised_usd:,.0f}/${program.credit_limit_usd:,.0f})"
                ),
                invoice_id=invoice.id, program_id=program.id,
            )

    for company in (invoice.buyer, invoice.seller):
        for kind in (product, GLOBAL):
            cl = db.query(models.CreditLimit).filter(
                models.CreditLimit.company_id == company.id,
                models.CreditLimit.product == kind,
            ).one_or_none()
            if cl is not None:
                cl.utilised_usd = round(cl.utilised_usd + amount, 2)
    db.flush()
    log_event(
        db, agent="credit_limit_agent", action="LIMITS_RESERVED",
        node="credit_limit_agent",
        message=f"Reserved ${amount:,.0f} against buyer/seller limits",
        invoice_id=invoice.id,
    )
    return {"ok": True, "reserved_usd": amount}


# ---------------- Tool builders ----------------

def build_credit_limit_tools(db: Session):
    TOOL_ensure_limits = StructuredTool.from_function(
        name="ensure_limits",
        description="Make sure a company has GLOBAL + product-specific credit limits. Idempotent.",
        args_schema=EnsureLimitsArgs,
        func=lambda company_id, product: tool_ensure_limits(db, company_id=company_id, product=product),
    )
    TOOL_hierarchical_headroom = StructuredTool.from_function(
        name="hierarchical_headroom",
        description=(
            "Compute the tightest available headroom (USD) across a company AND all "
            "ancestors, checking both the GLOBAL and product-specific limits at each "
            "level. Returns a per-level breakdown."
        ),
        args_schema=HeadroomArgs,
        func=lambda company_id, product: tool_hierarchical_headroom(db, company_id=company_id, product=product),
    )
    TOOL_reserve_limits = StructuredTool.from_function(
        name="reserve_limits",
        description="Reserve an approved invoice against program + buyer/seller limits.",
        args_schema=ReserveLimitsArgs,
        func=lambda invoice_id: tool_reserve_limits(db, invoice_id=invoice_id),
    )
    return TOOL_ensure_limits, TOOL_hierarchical_headroom, TOOL_reserve_limits


TOOL_ensure_limits = None
TOOL_hierarchical_headroom = None
TOOL_reserve_limits = None
