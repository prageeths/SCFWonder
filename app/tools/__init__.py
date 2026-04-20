"""Structured tools exposed to the SCF Wonder agents (LangChain StructuredTools).

Each tool is a self-contained, typed unit of domain logic. Agents (implemented
as LangGraph nodes) invoke these tools to interact with the database and each
other. Every tool writes a structured row to :class:`app.models.AgentEvent` so
the dashboard can render a full audit trail.

Exposed tool families:

* company_tools   — existence checks, onboarding (creates + profiles).
* underwriting_tools — risk profiling + new-program underwriting cases.
* credit_limit_tools — hierarchical limit maths, reservations.
* transaction_tools  — fee pricing, program lookups.
* review_tools    — temporary limit increase decisions.
"""
from .company_tools import (
    TOOL_lookup_company, TOOL_onboard_company, tool_lookup_company, tool_onboard_company,
)
from .underwriting_tools import (
    TOOL_build_risk_profile, TOOL_decide_new_program, tool_build_risk_profile,
    tool_decide_new_program,
)
from .credit_limit_tools import (
    TOOL_hierarchical_headroom, TOOL_reserve_limits, TOOL_ensure_limits,
    tool_hierarchical_headroom, tool_reserve_limits, tool_ensure_limits,
)
from .transaction_tools import (
    TOOL_find_program, TOOL_price_invoice, tool_find_program, tool_price_invoice,
)
from .review_tools import TOOL_decide_overage, tool_decide_overage

__all__ = [
    "TOOL_lookup_company", "TOOL_onboard_company",
    "TOOL_build_risk_profile", "TOOL_decide_new_program",
    "TOOL_hierarchical_headroom", "TOOL_reserve_limits", "TOOL_ensure_limits",
    "TOOL_find_program", "TOOL_price_invoice",
    "TOOL_decide_overage",
    # Plain callables (easy to call from graph nodes):
    "tool_lookup_company", "tool_onboard_company",
    "tool_build_risk_profile", "tool_decide_new_program",
    "tool_hierarchical_headroom", "tool_reserve_limits", "tool_ensure_limits",
    "tool_find_program", "tool_price_invoice",
    "tool_decide_overage",
]
