"""Tools used by the OnboardingAgent node."""
from __future__ import annotations

import datetime as _dt
import uuid
from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ._common import find_company_by_name, log_event


# ---------------- Schemas ----------------

class LookupCompanyArgs(BaseModel):
    name: str = Field(..., description="Company name to resolve (case-insensitive)")


class OnboardCompanyArgs(BaseModel):
    name: str = Field(..., min_length=2)
    country: str = Field(..., min_length=2, max_length=64)
    industry: Optional[str] = Field(default=None, max_length=128)
    annual_revenue_usd: float = Field(..., gt=0)
    years_operated: int = Field(..., ge=0, le=200)
    role: str = Field(default="BOTH")
    parent_name: Optional[str] = None


# ---------------- Implementations ----------------

def tool_lookup_company(db: Session, *, name: str) -> dict:
    """Resolve a company by name. Returns a JSON-serialisable dict."""
    c = find_company_by_name(db, name)
    if c is None:
        return {"found": False, "name": name}
    return {
        "found": True,
        "id": c.id,
        "name": c.name,
        "role": c.role,
        "country": c.country,
        "industry": c.industry,
        "annual_revenue_usd": c.annual_revenue_usd,
        "parent_id": c.parent_id,
        "founded_year": c.founded_year,
    }


def tool_onboard_company(
    db: Session,
    *,
    name: str,
    country: str,
    annual_revenue_usd: float,
    years_operated: int,
    industry: Optional[str] = None,
    role: str = "BOTH",
    parent_name: Optional[str] = None,
) -> dict:
    """Create a new company row. Does NOT profile risk or set limits — those
    are done by the Underwriter and Credit Limit tools so each step shows up
    as a distinct node in the graph."""
    # Dedup.
    existing = find_company_by_name(db, name)
    if existing is not None:
        return {"created": False, "company_id": existing.id, "reason": "already_exists"}

    role_norm = (role or "BOTH").strip().upper()
    if role_norm not in {"BUYER", "SELLER", "BOTH"}:
        role_norm = "BOTH"

    parent = find_company_by_name(db, parent_name) if parent_name else None
    founded_year = _dt.datetime.utcnow().year - int(years_operated)
    country_norm = country.strip().upper()[:2] if len(country.strip()) <= 3 else country.strip()

    company = models.Company(
        name=name.strip(),
        legal_name=name.strip(),
        country=country_norm,
        industry=(industry or "Industrial").strip(),
        role=role_norm,
        tax_id=f"EIN-{uuid.uuid4().hex[:8].upper()}",
        founded_year=founded_year,
        employees=max(1, int(annual_revenue_usd / 250_000)),
        annual_revenue_usd=float(annual_revenue_usd),
        description=f"Onboarded via the dashboard on {_dt.datetime.utcnow().date().isoformat()}.",
        parent_id=parent.id if parent else None,
    )
    db.add(company)
    db.flush()

    log_event(
        db,
        agent="onboarding_agent",
        action="COMPANY_CREATED",
        node="onboarding_agent",
        severity="DECISION",
        message=(
            f"Created {company.name} (role={role_norm}, country={country_norm}, "
            f"rev=${annual_revenue_usd:,.0f}, years={years_operated})"
        ),
        company_id=company.id,
        payload={
            "annual_revenue_usd": annual_revenue_usd,
            "years_operated": years_operated,
            "country": country_norm,
            "industry": company.industry,
        },
    )
    return {
        "created": True,
        "company_id": company.id,
        "name": company.name,
        "country": company.country,
        "industry": company.industry,
        "annual_revenue_usd": company.annual_revenue_usd,
        "founded_year": company.founded_year,
    }


# ---------------- StructuredTool wrappers ----------------
# These wrappers bind the DB session via a closure so agents can call the
# tool without passing the session around explicitly.

def build_company_tools(db: Session):
    TOOL_lookup_company = StructuredTool.from_function(
        name="lookup_company",
        description="Resolve a company by name. Returns `found`, `id`, `name`, `role`, `country`, `industry`, `annual_revenue_usd`.",
        args_schema=LookupCompanyArgs,
        func=lambda name: tool_lookup_company(db, name=name),
    )
    TOOL_onboard_company = StructuredTool.from_function(
        name="onboard_company",
        description=(
            "Create a new company row with the minimum underwriting inputs. "
            "This is the FIRST step of onboarding — it creates the record "
            "but does not yet assign a risk profile or credit limits."
        ),
        args_schema=OnboardCompanyArgs,
        func=lambda **kwargs: tool_onboard_company(db, **kwargs),
    )
    return TOOL_lookup_company, TOOL_onboard_company


# Module-level placeholders overwritten per-request by `build_company_tools`.
# They exist so `from app.tools import TOOL_lookup_company` works at import time.
TOOL_lookup_company = None
TOOL_onboard_company = None
