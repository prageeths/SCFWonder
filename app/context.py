"""Context engineering for the SCF Wonder agents.

Prompts are only as good as the facts they're given. These helpers gather
every piece of authoritative state an agent needs — company profile,
corporate hierarchy, current credit limits, existing programs, payment
history — and return it as a compact JSON-serialisable dict. Nothing in
this module makes decisions; it only assembles facts.

Each helper is used by one or more agents:

* ``company_context``        — Underwriter (rating), onboarding narration
* ``joint_risk_context``     — Underwriter (new program decision)
* ``program_context``        — Review (overage), UI explainability
* ``payment_history``        — Underwriter, Review (track record)
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models
from .config import (
    COUNTRY_RISK, INDUSTRY_RISK, PROGRAM_FUNDING_HARD_CEILING_USD,
    RATING_LADDER,
)


# ---------------------------------------------------------------------------
# Company-centric context
# ---------------------------------------------------------------------------


def _years_operated(company: models.Company) -> Optional[int]:
    if not company.founded_year:
        return None
    return max(0, _dt.datetime.utcnow().year - int(company.founded_year))


def _ancestors(company: models.Company) -> List[models.Company]:
    chain: List[models.Company] = []
    cursor: Optional[models.Company] = company
    seen = set()
    while cursor is not None and cursor.id not in seen:
        chain.append(cursor)
        seen.add(cursor.id)
        cursor = cursor.parent
    return chain


def _hierarchy_snapshot(company: models.Company) -> Dict[str, Any]:
    """Describe the corporate tree around ``company`` in plain language."""
    chain = _ancestors(company)
    ancestors_summary = [
        {
            "name": c.name, "country": c.country, "industry": c.industry,
            "role": c.role,
            "annual_revenue_usd": c.annual_revenue_usd,
        }
        for c in chain[1:]  # skip self
    ]
    return {
        "self": chain[0].name,
        "parent_chain": [c.name for c in chain[1:]],
        "max_revenue_in_tree": max(
            (c.annual_revenue_usd or 0.0) for c in chain
        ),
        "ancestors_detail": ancestors_summary,
        "children": [
            {"name": ch.name, "country": ch.country,
             "annual_revenue_usd": ch.annual_revenue_usd}
            for ch in getattr(company, "children", [])
        ],
    }


def _credit_limits_snapshot(company: models.Company) -> List[Dict[str, Any]]:
    return [
        {
            "product": cl.product,
            "limit_usd": cl.limit_usd,
            "utilised_usd": cl.utilised_usd,
            "headroom_usd": round(cl.limit_usd - cl.utilised_usd, 2),
            "utilisation_pct": round(
                100.0 * cl.utilised_usd / cl.limit_usd, 2
            ) if cl.limit_usd else None,
        }
        for cl in company.credit_limits
    ]


def _programs_snapshot(db: Session, company_id: int) -> Dict[str, Any]:
    """Summary of every program the company is in — as buyer AND as seller."""
    progs = db.query(models.Program).filter(
        (models.Program.buyer_id == company_id)
        | (models.Program.seller_id == company_id)
    ).all()
    as_buyer = [p for p in progs if p.buyer_id == company_id]
    as_seller = [p for p in progs if p.seller_id == company_id]

    def _fmt(p: models.Program, counterparty: str) -> Dict[str, Any]:
        return {
            "program_id": p.id, "name": p.name, "product": p.product,
            "counterparty": counterparty,
            "limit_usd": p.credit_limit_usd,
            "utilised_usd": p.utilised_usd,
            "headroom_usd": round(p.credit_limit_usd - p.utilised_usd, 2),
            "status": p.status,
        }

    return {
        "count_as_buyer": len(as_buyer),
        "count_as_seller": len(as_seller),
        "total_limit_usd_as_buyer": round(
            sum(p.credit_limit_usd for p in as_buyer), 2
        ),
        "total_limit_usd_as_seller": round(
            sum(p.credit_limit_usd for p in as_seller), 2
        ),
        "total_utilised_usd_as_buyer": round(
            sum(p.utilised_usd for p in as_buyer), 2
        ),
        "total_utilised_usd_as_seller": round(
            sum(p.utilised_usd for p in as_seller), 2
        ),
        "top_5_programs_as_buyer": [
            _fmt(p, p.seller.name)
            for p in sorted(as_buyer, key=lambda x: -x.credit_limit_usd)[:5]
        ],
        "top_5_programs_as_seller": [
            _fmt(p, p.buyer.name)
            for p in sorted(as_seller, key=lambda x: -x.credit_limit_usd)[:5]
        ],
    }


def _payment_history_snapshot(db: Session, company_id: int) -> Dict[str, Any]:
    """Aggregate invoice outcomes across all programs this company is in."""
    q = db.query(
        models.Invoice.status,
        func.count(models.Invoice.id),
        func.sum(models.Invoice.amount_usd),
    ).filter(
        (models.Invoice.buyer_id == company_id)
        | (models.Invoice.seller_id == company_id)
    ).group_by(models.Invoice.status)

    totals: Dict[str, Dict[str, float]] = {}
    total_count = 0
    total_amount = 0.0
    for status, cnt, amt in q.all():
        totals[status] = {
            "count": int(cnt or 0),
            "amount_usd": round(float(amt or 0.0), 2),
        }
        total_count += int(cnt or 0)
        total_amount += float(amt or 0.0)

    # Simple derived ratios the LLM can reason about.
    paid_cnt = totals.get("PAID", {}).get("count", 0)
    funded_cnt = totals.get("FUNDED", {}).get("count", 0)
    rejected_cnt = totals.get("REJECTED", {}).get("count", 0)
    settled_cnt = paid_cnt + funded_cnt
    ratios = {
        "total_invoice_count": total_count,
        "total_amount_usd": round(total_amount, 2),
        "paid_pct_of_total": round(100.0 * paid_cnt / total_count, 2) if total_count else None,
        "settled_pct_of_total": round(100.0 * settled_cnt / total_count, 2) if total_count else None,
        "rejected_pct_of_total": round(100.0 * rejected_cnt / total_count, 2) if total_count else None,
    }
    return {"by_status": totals, "ratios": ratios}


def company_context(db: Session, company: models.Company) -> Dict[str, Any]:
    """Everything the Underwriter needs to rate this single company."""
    return {
        "identity": {
            "id": company.id, "name": company.name,
            "legal_name": company.legal_name,
            "country": company.country, "industry": company.industry,
            "role": company.role,
            "founded_year": company.founded_year,
            "years_operated": _years_operated(company),
            "employees": company.employees,
            "annual_revenue_usd": company.annual_revenue_usd,
        },
        "risk_benchmarks": {
            "industry_benchmark_risk": INDUSTRY_RISK.get(company.industry or "", 0.004),
            "country_benchmark_risk": COUNTRY_RISK.get(company.country or "US", 0.002),
            "rating_ladder_best_to_worst": RATING_LADDER,
        },
        "hierarchy": _hierarchy_snapshot(company),
        "credit_limits": _credit_limits_snapshot(company),
        "programs": _programs_snapshot(db, company.id),
        "payment_history": _payment_history_snapshot(db, company.id),
    }


# ---------------------------------------------------------------------------
# Joint / program context (used by Underwriter + Review)
# ---------------------------------------------------------------------------


def joint_risk_context(
    db: Session,
    invoice: models.Invoice,
    *,
    buyer_head: float,
    seller_head: float,
    buyer_breakdown: Dict[str, float],
    seller_breakdown: Dict[str, float],
    requested_limit_usd: float,
) -> Dict[str, Any]:
    """Full situation snapshot for the Underwriter's decide_new_program call."""
    buyer_rp = invoice.buyer.risk_profile
    seller_rp = invoice.seller.risk_profile
    return {
        "invoice": {
            "id": invoice.id,
            "amount_usd": invoice.amount_usd,
            "currency": invoice.currency,
            "amount_native": invoice.amount,
            "product": invoice.product,
            "tenor_days": invoice.tenor_days,
            "grace_period_days": invoice.grace_period_days,
        },
        "buyer": company_context(db, invoice.buyer),
        "seller": company_context(db, invoice.seller),
        "ratings_view": {
            "buyer_rating": buyer_rp.rating if buyer_rp else None,
            "seller_rating": seller_rp.rating if seller_rp else None,
            "buyer_pd_1y": buyer_rp.pd_1y if buyer_rp else None,
            "seller_pd_1y": seller_rp.pd_1y if seller_rp else None,
            "buyer_credit_spread": buyer_rp.credit_spread if buyer_rp else None,
            "seller_credit_spread": seller_rp.credit_spread if seller_rp else None,
            "rating_ladder_best_to_worst": RATING_LADDER,
            "hard_policy": (
                "APPROVE requires buyer rating to be BBB or better "
                "AND seller rating to be BB or better."
            ),
        },
        "hierarchical_headrooms": {
            "buyer_subtree_headroom_usd": buyer_head,
            "seller_subtree_headroom_usd": seller_head,
            "buyer_breakdown": buyer_breakdown,
            "seller_breakdown": seller_breakdown,
        },
        "ceiling": {
            "program_funding_max_usd": PROGRAM_FUNDING_HARD_CEILING_USD,
            "requested_limit_usd_before_ceiling": requested_limit_usd,
        },
    }


def program_context(db: Session, program: models.Program) -> Dict[str, Any]:
    """Summary of a bilateral program for the Review agent."""
    # Recent invoices on this program.
    recent = (
        db.query(models.Invoice)
        .filter(models.Invoice.program_id == program.id)
        .order_by(models.Invoice.id.desc())
        .limit(25)
        .all()
    )
    by_status: Dict[str, Dict[str, float]] = {}
    for inv in recent:
        b = by_status.setdefault(inv.status, {"count": 0, "amount_usd": 0.0})
        b["count"] += 1
        b["amount_usd"] = round(b["amount_usd"] + (inv.amount_usd or 0.0), 2)
    total = len(recent)
    paid = sum(1 for i in recent if i.status == "PAID")
    funded = sum(1 for i in recent if i.status == "FUNDED")
    rejected = sum(1 for i in recent if i.status == "REJECTED")
    return {
        "program": {
            "id": program.id, "name": program.name, "product": program.product,
            "credit_limit_usd": program.credit_limit_usd,
            "utilised_usd": program.utilised_usd,
            "headroom_usd": round(program.credit_limit_usd - program.utilised_usd, 2),
            "utilisation_pct": round(
                100.0 * program.utilised_usd / program.credit_limit_usd, 2
            ) if program.credit_limit_usd else None,
            "status": program.status,
            "base_currency": program.base_currency,
            "grace_period_days": program.grace_period_days,
        },
        "program_payment_history_last_25": {
            "by_status": by_status,
            "ratios": {
                "total_invoice_count": total,
                "paid_pct": round(100.0 * paid / total, 2) if total else None,
                "settled_pct": round(100.0 * (paid + funded) / total, 2) if total else None,
                "rejected_pct": round(100.0 * rejected / total, 2) if total else None,
            },
        },
    }
