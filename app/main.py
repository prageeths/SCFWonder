"""FastAPI entry point for SCF Wonder."""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from . import models, schemas
from .config import (
    ALLOWED_TENORS, APP_NAME, APP_TAGLINE, BASE_RATE, COUNTRIES, FX_TO_USD,
    INDUSTRIES, LLM_MODEL, PRODUCT_FACTORING, PRODUCT_REVERSE_FACTORING,
    PROGRAM_FUNDING_HARD_CEILING_USD, RATING_LADDER, SUPPORTED_CURRENCIES,
    llm_enabled,
)
from .database import SessionLocal, init_db
from .graph import run_invoice_flow
from .tools.company_tools import tool_onboard_company
from .tools.credit_limit_tools import tool_ensure_limits
from .tools.underwriting_tools import tool_build_risk_profile


app = FastAPI(
    title=f"{APP_NAME} — {APP_TAGLINE}",
    version="1.0.0",
    description=(
        "Agentic supply-chain finance platform. Each invoice is processed by a "
        "LangGraph state machine that orchestrates six cooperating agents "
        "(Orchestrator, Onboarding, Underwriter, Credit Limit, Transaction, "
        "Review) using LangChain StructuredTools."
    ),
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    return FileResponse(WEB_DIR / "templates" / "index.html")


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


@app.get("/api/meta")
def meta() -> dict:
    return {
        "app": APP_NAME,
        "tagline": APP_TAGLINE,
        "base_rate": BASE_RATE,
        "base_rate_pct": f"{BASE_RATE * 100:.2f}%",
        "currencies": SUPPORTED_CURRENCIES,
        "tenors": ALLOWED_TENORS,
        "products": [
            {"value": PRODUCT_FACTORING, "label": "Factoring"},
            {"value": PRODUCT_REVERSE_FACTORING, "label": "Reverse Factoring"},
        ],
        "countries": [{"code": c, "name": n} for c, n in COUNTRIES],
        "industries": INDUSTRIES,
        "rating_ladder": RATING_LADDER,
        "fx_to_usd": FX_TO_USD,
        "guardrails": {
            "program_funding_max_usd": PROGRAM_FUNDING_HARD_CEILING_USD,
            "hierarchy_invariant_enforced": True,
            "llm_enabled": llm_enabled(),
            "llm_model": LLM_MODEL if llm_enabled() else None,
        },
        "as_of": _dt.datetime.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


def _company_mini(c: models.Company) -> dict:
    return {
        "id": c.id, "name": c.name, "role": c.role,
        "country": c.country, "industry": c.industry,
    }


def _company_row(c: models.Company) -> dict:
    rp = c.risk_profile
    return {
        "id": c.id, "name": c.name, "role": c.role,
        "country": c.country, "industry": c.industry,
        "annual_revenue_usd": c.annual_revenue_usd,
        "rating": rp.rating if rp else None,
        "credit_spread": rp.credit_spread if rp else None,
        "pd_1y": rp.pd_1y if rp else None,
        "parent_name": c.parent.name if c.parent else None,
    }


@app.get("/api/companies/exists")
def company_exists(
    name: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> dict:
    norm = name.strip()
    exact = db.query(models.Company).filter(models.Company.name == norm).one_or_none()
    if exact is None:
        exact = db.query(models.Company).filter(models.Company.name.ilike(norm)).first()
    suggestions = []
    if exact is None:
        rows = (
            db.query(models.Company)
            .filter(models.Company.name.ilike(f"%{norm}%"))
            .order_by(models.Company.name.asc())
            .limit(5)
            .all()
        )
        suggestions = [_company_row(c) for c in rows]
    return {
        "exists": exact is not None,
        "company": _company_row(exact) if exact else None,
        "suggestions": suggestions,
    }


@app.get("/api/companies/search")
def search_companies(
    q: str = Query("", max_length=255),
    role: Optional[str] = None,
    limit: int = 20,
    db: Session = Depends(get_db),
) -> List[dict]:
    query = db.query(models.Company)
    if q:
        query = query.filter(models.Company.name.ilike(f"%{q}%"))
    if role:
        query = query.filter(models.Company.role.in_([role.upper(), "BOTH"]))
    rows = query.order_by(models.Company.name.asc()).limit(limit).all()
    return [_company_mini(c) for c in rows]


@app.get("/api/companies")
def list_companies(
    role: Optional[str] = None,
    rating: Optional[str] = None,
    q: Optional[str] = None,
    sort: str = "name",
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    query = db.query(models.Company)
    if role:
        query = query.filter(models.Company.role.in_([role.upper(), "BOTH"]))
    if q:
        query = query.filter(models.Company.name.ilike(f"%{q}%"))
    if rating:
        query = query.join(models.RiskProfile).filter(
            models.RiskProfile.rating == rating.upper()
        )
    total = query.count()
    if sort == "revenue":
        query = query.order_by(models.Company.annual_revenue_usd.desc().nullslast())
    elif sort == "rating":
        rating_rank = case(
            {r: i for i, r in enumerate(RATING_LADDER)},
            value=models.RiskProfile.rating,
            else_=99,
        )
        query = query.outerjoin(models.RiskProfile).order_by(
            rating_rank.asc(), models.Company.name.asc()
        )
    else:
        query = query.order_by(models.Company.name.asc())
    rows = query.offset(offset).limit(limit).all()
    return {"total": total, "items": [_company_row(c) for c in rows]}


@app.get("/api/companies/{company_id}")
def company_detail(company_id: int, db: Session = Depends(get_db)) -> dict:
    company = db.query(models.Company).get(company_id)
    if not company:
        raise HTTPException(404, f"Company {company_id} not found")
    programs = (
        db.query(models.Program)
        .filter((models.Program.buyer_id == company_id)
                | (models.Program.seller_id == company_id))
        .order_by(models.Program.id.desc())
        .all()
    )
    rp = company.risk_profile
    return {
        "id": company.id, "name": company.name,
        "legal_name": company.legal_name, "country": company.country,
        "industry": company.industry, "role": company.role,
        "description": company.description, "website": company.website,
        "annual_revenue_usd": company.annual_revenue_usd,
        "employees": company.employees, "founded_year": company.founded_year,
        "tax_id": company.tax_id,
        "parent": _company_mini(company.parent) if company.parent else None,
        "children": [_company_mini(ch) for ch in company.children],
        "risk_profile": (
            {
                "rating": rp.rating, "pd_1y": rp.pd_1y,
                "credit_spread": rp.credit_spread,
                "industry_risk": rp.industry_risk, "country_risk": rp.country_risk,
                "tenure_years": rp.tenure_years,
                "leverage_score": rp.leverage_score,
                "notes": rp.notes,
                "last_reviewed": rp.last_reviewed.isoformat(),
            } if rp else None
        ),
        "credit_limits": [
            {"product": cl.product, "limit_usd": cl.limit_usd,
             "utilised_usd": cl.utilised_usd,
             "headroom_usd": round(cl.limit_usd - cl.utilised_usd, 2)}
            for cl in company.credit_limits
        ],
        "programs": [
            {"id": p.id, "name": p.name, "product": p.product,
             "buyer": _company_mini(p.buyer), "seller": _company_mini(p.seller),
             "credit_limit_usd": p.credit_limit_usd,
             "utilised_usd": p.utilised_usd, "status": p.status}
            for p in programs
        ],
    }


@app.post("/api/companies")
def create_company(
    payload: schemas.CompanyOnboard,
    db: Session = Depends(get_db),
) -> dict:
    """Manual onboarding: creates the company, runs the Underwriter Agent and
    Credit Limit Agent tools. Returns the full profile + the agent trace."""
    res = tool_onboard_company(db, **payload.model_dump())
    if not res.get("created") and not res.get("company_id"):
        raise HTTPException(400, res.get("reason") or "onboarding failed")
    cid = res["company_id"]
    rp = tool_build_risk_profile(db, company_id=cid)
    tool_ensure_limits(db, company_id=cid, product=PRODUCT_FACTORING)
    tool_ensure_limits(db, company_id=cid, product=PRODUCT_REVERSE_FACTORING)
    db.commit()

    events = (
        db.query(models.AgentEvent)
        .filter(models.AgentEvent.company_id == cid)
        .order_by(models.AgentEvent.id.asc())
        .all()
    )
    return {
        "company": company_detail(cid, db),
        "risk_profile_tool_result": rp,
        "events": [
            {"id": e.id, "timestamp": e.timestamp.isoformat(),
             "agent": e.agent, "action": e.action, "node": e.node,
             "severity": e.severity, "message": e.message}
            for e in events
        ],
    }


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------


@app.get("/api/programs")
def list_programs(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)) -> dict:
    q = db.query(models.Program)
    total = q.count()
    rows = q.order_by(models.Program.id.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "items": [
            {"id": p.id, "name": p.name, "product": p.product,
             "buyer": _company_mini(p.buyer), "seller": _company_mini(p.seller),
             "credit_limit_usd": p.credit_limit_usd,
             "utilised_usd": p.utilised_usd, "status": p.status}
            for p in rows
        ],
    }


@app.get("/api/programs/{program_id}/facility")
def program_facility(program_id: int, db: Session = Depends(get_db)) -> dict:
    from .tools.credit_limit_tools import tool_hierarchical_headroom

    program = db.query(models.Program).get(program_id)
    if not program:
        raise HTTPException(404, f"Program {program_id} not found")

    invoices = (
        db.query(models.Invoice)
        .filter(models.Invoice.program_id == program_id)
        .order_by(models.Invoice.id.desc())
        .all()
    )
    open_invoices = [i for i in invoices if i.status in ("FUNDED", "APPROVED", "REVIEW")]
    by_currency: dict = {}
    for inv in open_invoices:
        b = by_currency.setdefault(inv.currency, {
            "currency": inv.currency, "count": 0, "amount_native": 0.0,
            "fx_to_usd": FX_TO_USD.get(inv.currency, 1.0), "amount_usd": 0.0,
        })
        b["count"] += 1
        b["amount_native"] = round(b["amount_native"] + inv.amount, 2)
        b["amount_usd"] = round(b["amount_usd"] + inv.amount_usd, 2)

    total_open = round(sum(b["amount_usd"] for b in by_currency.values()), 2)

    program_limit = program.credit_limit_usd
    program_used = program.utilised_usd
    program_headroom = round(program_limit - program_used, 2)
    buyer_res = tool_hierarchical_headroom(db, company_id=program.buyer_id, product=program.product)
    seller_res = tool_hierarchical_headroom(db, company_id=program.seller_id, product=program.product)
    buyer_headroom = buyer_res["headroom_usd"]
    seller_headroom = seller_res["headroom_usd"]
    binding = min(program_headroom, buyer_headroom, seller_headroom)
    if binding == program_headroom:
        binding_constraint = "program_limit"
    elif binding == buyer_headroom:
        binding_constraint = "buyer_hierarchical_limit"
    else:
        binding_constraint = "seller_hierarchical_limit"

    explanation = [
        {"step": 1, "title": "Normalise every invoice to USD",
         "detail": (f"{len(open_invoices)} live invoices across {len(by_currency)} currencies "
                    f"are FX-converted before any limit math; total USD exposure = "
                    f"${total_open:,.2f}.")},
        {"step": 2, "title": "Bilateral program limit",
         "detail": (f"Program '{program.name}' carries a ${program_limit:,.2f} limit; "
                    f"utilised ${program_used:,.2f}; headroom ${program_headroom:,.2f}.")},
        {"step": 3, "title": "Buyer hierarchical envelope",
         "detail": f"Tightest buyer-side headroom across the buyer tree: ${buyer_headroom:,.2f}."},
        {"step": 4, "title": "Seller hierarchical envelope",
         "detail": f"Tightest seller-side headroom: ${seller_headroom:,.2f}."},
        {"step": 5, "title": "Binding constraint",
         "detail": f"A new invoice's USD equivalent must be ≤ {binding_constraint} at "
                   f"${binding:,.2f}."},
    ]

    return {
        "program": {
            "id": program.id, "name": program.name, "product": program.product,
            "buyer": _company_mini(program.buyer),
            "seller": _company_mini(program.seller),
            "credit_limit_usd": program_limit,
            "utilised_usd": program_used, "headroom_usd": program_headroom,
            "status": program.status,
        },
        "fx_snapshot": FX_TO_USD,
        "open_invoices_by_currency": list(by_currency.values()),
        "totals": {
            "open_invoice_count": len(open_invoices),
            "open_amount_usd": total_open,
            "program_limit_usd": program_limit,
            "program_utilised_usd": program_used,
            "program_headroom_usd": program_headroom,
            "buyer_subtree_headroom_usd": buyer_headroom,
            "seller_subtree_headroom_usd": seller_headroom,
            "binding_headroom_usd": binding,
            "binding_constraint": binding_constraint,
        },
        "buyer_hierarchy_breakdown": buyer_res["breakdown"],
        "seller_hierarchy_breakdown": seller_res["breakdown"],
        "explanation": explanation,
    }


# ---------------------------------------------------------------------------
# Invoices (runs the agent graph)
# ---------------------------------------------------------------------------


def _invoice_dict(inv: models.Invoice) -> dict:
    return {
        "id": inv.id, "invoice_number": inv.invoice_number,
        "seller": _company_mini(inv.seller), "buyer": _company_mini(inv.buyer),
        "product": inv.product, "amount": inv.amount, "currency": inv.currency,
        "amount_usd": inv.amount_usd, "tenor_days": inv.tenor_days,
        "grace_period_days": inv.grace_period_days,
        "issue_date": inv.issue_date.isoformat(),
        "due_date": inv.due_date.isoformat(),
        "base_rate": inv.base_rate, "credit_spread": inv.credit_spread,
        "fee_usd": inv.fee_usd, "funded_amount_usd": inv.funded_amount_usd,
        "status": inv.status, "decision_reason": inv.decision_reason,
        "program_id": inv.program_id,
    }


@app.post("/api/invoices")
def submit_invoice(
    payload: schemas.InvoiceCreate, db: Session = Depends(get_db)
) -> dict:
    """Invokes the LangGraph. Returns the final invoice, the graph trace, and
    every tool call made along the way."""
    result = run_invoice_flow(db, payload.model_dump())

    # If the graph rejected before creating an invoice (e.g. missing onboarding
    # payload), surface that as a structured 422 so the UI can prompt.
    if result.invoice_id is None:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invoice_not_created",
                "message": result.decision_reason or "Invoice could not be created.",
                "trace": result.trace,
                "tool_calls": result.tool_calls,
            },
        )

    inv = db.query(models.Invoice).get(result.invoice_id)
    events = (
        db.query(models.AgentEvent)
        .filter(models.AgentEvent.invoice_id == inv.id)
        .order_by(models.AgentEvent.id.asc())
        .all()
    )
    return {
        "invoice": _invoice_dict(inv),
        "summary": f"{inv.invoice_number}: {inv.status} — {inv.decision_reason}",
        "trace": result.trace,
        "tool_calls": result.tool_calls,
        "events": [
            {"id": e.id, "timestamp": e.timestamp.isoformat(),
             "agent": e.agent, "action": e.action, "node": e.node,
             "severity": e.severity, "message": e.message}
            for e in events
        ],
    }


@app.get("/api/invoices")
def list_invoices(
    status: Optional[str] = None,
    company_id: Optional[int] = None,
    limit: int = 50, offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(models.Invoice)
    if status:
        q = q.filter(models.Invoice.status == status.upper())
    if company_id:
        q = q.filter((models.Invoice.buyer_id == company_id) | (models.Invoice.seller_id == company_id))
    total = q.count()
    rows = q.order_by(models.Invoice.id.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": [_invoice_dict(i) for i in rows]}


@app.get("/api/invoices/{invoice_id}")
def invoice_detail(invoice_id: int, db: Session = Depends(get_db)) -> dict:
    inv = db.query(models.Invoice).get(invoice_id)
    if not inv:
        raise HTTPException(404, f"Invoice {invoice_id} not found")
    events = (
        db.query(models.AgentEvent)
        .filter(models.AgentEvent.invoice_id == inv.id)
        .order_by(models.AgentEvent.id.asc())
        .all()
    )
    return {
        "invoice": _invoice_dict(inv),
        "events": [
            {"id": e.id, "timestamp": e.timestamp.isoformat(),
             "agent": e.agent, "action": e.action, "node": e.node,
             "severity": e.severity, "message": e.message}
            for e in events
        ],
    }


# ---------------------------------------------------------------------------
# Agent event console + dashboard summaries
# ---------------------------------------------------------------------------


@app.get("/api/events")
def list_events(
    agent: Optional[str] = None, severity: Optional[str] = None,
    node: Optional[str] = None,
    limit: int = 100, offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(models.AgentEvent)
    if agent:
        q = q.filter(models.AgentEvent.agent == agent)
    if severity:
        q = q.filter(models.AgentEvent.severity == severity.upper())
    if node:
        q = q.filter(models.AgentEvent.node == node)
    total = q.count()
    rows = q.order_by(models.AgentEvent.id.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "items": [
            {"id": e.id, "timestamp": e.timestamp.isoformat(),
             "agent": e.agent, "action": e.action, "node": e.node,
             "severity": e.severity, "message": e.message,
             "invoice_id": e.invoice_id, "company_id": e.company_id,
             "program_id": e.program_id}
            for e in rows
        ],
    }


@app.get("/api/summary")
def summary(db: Session = Depends(get_db)) -> dict:
    by_status: dict = {}
    for s, cnt, amt in db.query(
        models.Invoice.status, func.count(models.Invoice.id), func.sum(models.Invoice.amount_usd)
    ).group_by(models.Invoice.status).all():
        by_status[s] = {"count": int(cnt or 0), "amount_usd": round(float(amt or 0.0), 2)}
    by_product: dict = {}
    for p, cnt, amt in db.query(
        models.Invoice.product, func.count(models.Invoice.id), func.sum(models.Invoice.amount_usd)
    ).group_by(models.Invoice.product).all():
        by_product[p] = {"count": int(cnt or 0), "amount_usd": round(float(amt or 0.0), 2)}

    recent_decisions = (
        db.query(models.AgentEvent)
        .filter(models.AgentEvent.severity == "DECISION")
        .order_by(models.AgentEvent.id.desc())
        .limit(20).all()
    )
    return {
        "base_rate": BASE_RATE,
        "as_of": _dt.datetime.utcnow().isoformat(),
        "totals": {
            "companies": db.query(models.Company).count(),
            "programs": db.query(models.Program).count(),
            "invoices": db.query(models.Invoice).count(),
            "agent_events": db.query(models.AgentEvent).count(),
        },
        "by_status": by_status,
        "by_product": by_product,
        "recent_decisions": [
            {"id": e.id, "timestamp": e.timestamp.isoformat(),
             "agent": e.agent, "action": e.action, "node": e.node,
             "message": e.message, "invoice_id": e.invoice_id,
             "company_id": e.company_id}
            for e in recent_decisions
        ],
    }


@app.get("/api/transactions/summary")
def transactions_summary(db: Session = Depends(get_db)) -> dict:
    total = db.query(func.count(models.Invoice.id)).scalar() or 0
    total_amount = db.query(func.sum(models.Invoice.amount_usd)).scalar() or 0.0
    total_fees = db.query(func.sum(models.Invoice.fee_usd)).scalar() or 0.0
    total_funded = db.query(func.sum(models.Invoice.funded_amount_usd)).scalar() or 0.0
    by_status = {}
    for s, c, a, f in db.query(
        models.Invoice.status, func.count(models.Invoice.id),
        func.sum(models.Invoice.amount_usd), func.sum(models.Invoice.fee_usd)
    ).group_by(models.Invoice.status).all():
        by_status[s] = {"count": int(c or 0), "amount_usd": round(float(a or 0.0), 2),
                        "fee_usd": round(float(f or 0.0), 2)}
    by_product = {}
    for p, c, a, f in db.query(
        models.Invoice.product, func.count(models.Invoice.id),
        func.sum(models.Invoice.amount_usd), func.sum(models.Invoice.fee_usd)
    ).group_by(models.Invoice.product).all():
        by_product[p] = {"count": int(c or 0), "amount_usd": round(float(a or 0.0), 2),
                         "fee_usd": round(float(f or 0.0), 2)}
    by_currency = {}
    for cur, c, a in db.query(
        models.Invoice.currency, func.count(models.Invoice.id),
        func.sum(models.Invoice.amount)
    ).group_by(models.Invoice.currency).all():
        by_currency[cur] = {"count": int(c or 0), "amount_native": round(float(a or 0.0), 2)}

    top_rows = (
        db.query(models.Invoice.program_id, func.count(models.Invoice.id),
                 func.sum(models.Invoice.amount_usd))
        .filter(models.Invoice.program_id.isnot(None))
        .group_by(models.Invoice.program_id)
        .order_by(func.sum(models.Invoice.amount_usd).desc())
        .limit(10).all()
    )
    top_programs = []
    for pid, cnt, amt in top_rows:
        prog = db.query(models.Program).get(pid)
        if prog is None:
            continue
        top_programs.append({
            "program_id": pid, "name": prog.name, "product": prog.product,
            "buyer": _company_mini(prog.buyer), "seller": _company_mini(prog.seller),
            "invoice_count": int(cnt or 0), "amount_usd": round(float(amt or 0.0), 2),
        })
    recent_invoices = (
        db.query(models.Invoice).order_by(models.Invoice.id.desc()).limit(25).all()
    )
    return {
        "as_of": _dt.datetime.utcnow().isoformat(),
        "base_rate": BASE_RATE,
        "totals": {
            "invoice_count": int(total),
            "amount_usd": round(float(total_amount), 2),
            "fee_usd": round(float(total_fees), 2),
            "funded_usd": round(float(total_funded), 2),
        },
        "by_status": by_status, "by_product": by_product, "by_currency": by_currency,
        "top_programs": top_programs,
        "recent_invoices": [_invoice_dict(i) for i in recent_invoices],
    }
