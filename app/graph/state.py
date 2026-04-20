"""Shared state for the SCF Wonder LangGraph."""
from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict


class WonderState(TypedDict, total=False):
    """State that flows through the agent graph.

    Any field declared with ``Annotated[..., operator.add]`` is merged
    (rather than replaced) when two nodes write to it. That lets the
    agent trace and structured tool_calls accumulate across the whole run.
    """

    # ---- input ----
    request: Dict[str, Any]     # raw invoice payload from the API
    run_id: str

    # ---- resolved entities ----
    seller_id: Optional[int]
    buyer_id: Optional[int]
    seller_name: Optional[str]
    buyer_name: Optional[str]
    invoice_id: Optional[int]
    program_id: Optional[int]

    # ---- onboarding bookkeeping ----
    pending_onboard_seller: Optional[Dict[str, Any]]
    pending_onboard_buyer: Optional[Dict[str, Any]]

    # ---- routing decisions ----
    next_step: str              # "underwriting", "review", "approved", "rejected"
    headroom_usd: Optional[float]
    program_headroom_usd: Optional[float]
    buyer_headroom_usd: Optional[float]
    seller_headroom_usd: Optional[float]
    overage_usd: Optional[float]

    # ---- outputs ----
    status: Optional[str]
    decision_reason: Optional[str]
    fee_usd: Optional[float]
    funded_amount_usd: Optional[float]

    # ---- traces (append-only) ----
    trace: Annotated[List[Dict[str, Any]], operator.add]
    tool_calls: Annotated[List[Dict[str, Any]], operator.add]
    errors: Annotated[List[str], operator.add]
