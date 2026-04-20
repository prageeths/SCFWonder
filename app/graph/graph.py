"""Build and run the SCF Wonder LangGraph.

Graph topology::

    orchestrator
        │
        ▼
    onboarding_agent ──► create_invoice ──► ensure_limits
                                               │
                                               ▼
                                          find_program
                                         /            \\
                               (no program)         (program)
                                   │                    │
                                   ▼                    ▼
                       underwrite_program ──► limit_check
                         │         │             │   │   │
                     (decl)      (appr)      (hard) (review)(ok)
                         ▼          ▼         ▼       ▼     ▼
                      rejected  limit_check rejected review approve
                                                │       │        │
                                                ▼       ▼        ▼
                                            (same)   approve   (funded)
                                                        │
                                                        ▼
                                                    finalise

Rejections at any stage route to ``finalise``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from langgraph.graph import END, StateGraph
from sqlalchemy.orm import Session

from . import nodes
from .state import WonderState


@dataclass
class GraphResult:
    state: Dict[str, Any]
    invoice_id: Optional[int]
    status: str
    decision_reason: Optional[str]
    trace: List[Dict[str, Any]]
    tool_calls: List[Dict[str, Any]]


def build_graph(db: Session):
    builder = StateGraph(WonderState)

    builder.add_node("orchestrator", nodes.make_orchestrator_node(db))
    builder.add_node("onboarding_agent", nodes.make_onboarding_node(db))
    builder.add_node("create_invoice", nodes.make_create_invoice_node(db))
    builder.add_node("ensure_limits", nodes.make_ensure_limits_node(db))
    builder.add_node("find_program", nodes.make_find_program_node(db))
    builder.add_node("underwrite_program", nodes.make_underwrite_program_node(db))
    builder.add_node("limit_check", nodes.make_limit_check_node(db))
    builder.add_node("review", nodes.make_review_node(db))
    builder.add_node("approve", nodes.make_approve_node(db))
    builder.add_node("finalise", nodes.make_finalise_node(db))

    builder.set_entry_point("orchestrator")

    # Conditional routing based on state["next_step"]
    def _route(state: WonderState) -> str:
        return state.get("next_step") or "finalise"

    builder.add_conditional_edges(
        "orchestrator",
        _route,
        {
            "onboard_seller": "onboarding_agent",
            "rejected": "finalise",
        },
    )
    builder.add_conditional_edges(
        "onboarding_agent",
        _route,
        {
            "create_invoice": "create_invoice",
            "rejected": "finalise",
        },
    )
    builder.add_edge("create_invoice", "ensure_limits")
    builder.add_edge("ensure_limits", "find_program")
    builder.add_conditional_edges(
        "find_program",
        _route,
        {
            "limit_check": "limit_check",
            "underwrite_program": "underwrite_program",
        },
    )
    builder.add_conditional_edges(
        "underwrite_program",
        _route,
        {
            "limit_check": "limit_check",
            "rejected": "finalise",
        },
    )
    builder.add_conditional_edges(
        "limit_check",
        _route,
        {
            "approve": "approve",
            "review": "review",
            "rejected": "finalise",
        },
    )
    builder.add_conditional_edges(
        "review",
        _route,
        {
            "approve": "approve",
            "rejected": "finalise",
        },
    )
    builder.add_edge("approve", "finalise")
    builder.add_edge("finalise", END)

    return builder.compile()


def run_invoice_flow(db: Session, payload: Dict[str, Any]) -> GraphResult:
    """Execute the full agentic flow for a single invoice payload."""
    graph = build_graph(db)
    initial: WonderState = {
        "request": payload,
        "run_id": uuid.uuid4().hex,
        "trace": [],
        "tool_calls": [],
        "errors": [],
    }
    final_state = graph.invoke(initial)

    return GraphResult(
        state=dict(final_state),
        invoice_id=final_state.get("invoice_id"),
        status=final_state.get("status") or "UNKNOWN",
        decision_reason=final_state.get("decision_reason"),
        trace=final_state.get("trace") or [],
        tool_calls=final_state.get("tool_calls") or [],
    )
