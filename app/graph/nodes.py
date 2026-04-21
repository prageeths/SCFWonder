"""LangGraph node implementations.

Every node is a small function that reads :class:`WonderState`, calls one or
more LangChain StructuredTools, and returns a partial-state update. The
graph's shared reducers accumulate the trace and tool_calls across nodes.

Node names map 1:1 to the agent personas:

* ``orchestrator``      — intake, validation, parse the invoice payload.
* ``onboarding_agent``  — creates any missing counterparty.
* ``underwriter_agent`` — risk-profiles all parties and decides brand-new
  programs via the ``decide_new_program`` tool.
* ``credit_limit_agent`` — ensures limits and computes hierarchical headroom.
* ``transaction_agent`` — routes: approve / review / rejected; prices fees;
  reserves limits after an approval.
* ``review_agent``      — decides on program-limit overages.
* ``finalise``          — writes final status + decision_reason to the DB.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from .. import models
from ..config import (
    BASE_RATE, FX_TO_USD, PRODUCTS, PROGRAM_FUNDING_HARD_CEILING_USD,
    SUPPORTED_CURRENCIES, ALLOWED_TENORS, llm_enabled,
)
from ..llm import (
    SYSTEM_ONBOARDING, SYSTEM_ORCHESTRATOR, OnboardingSummary,
    OrchestratorSummary, facts_block, safe_structured_call,
)
from ..tools._common import find_company_by_name, log_event
from ..tools.company_tools import tool_lookup_company, tool_onboard_company
from ..tools.underwriting_tools import tool_build_risk_profile, tool_decide_new_program
from ..tools.credit_limit_tools import (
    tool_ensure_limits, tool_hierarchical_headroom, tool_reserve_limits,
)
from ..tools.transaction_tools import tool_find_program, tool_price_invoice
from ..tools.review_tools import tool_decide_overage
from .state import WonderState


def _append_trace(node: str, message: str, **payload) -> Dict[str, Any]:
    """Return a partial state update that appends a single trace entry."""
    entry = {
        "node": node,
        "timestamp": _dt.datetime.utcnow().isoformat(),
        "message": message,
        "payload": payload or None,
    }
    return {"trace": [entry]}


def _append_tool_call(node: str, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "tool_calls": [{
            "node": node,
            "tool": tool_name,
            "args": args,
            "result": result,
            "timestamp": _dt.datetime.utcnow().isoformat(),
        }]
    }


# ---------------------------------------------------------------------------
# 1. Orchestrator node
# ---------------------------------------------------------------------------

def make_orchestrator_node(db: Session) -> Callable[[WonderState], Dict[str, Any]]:
    def orchestrator(state: WonderState) -> Dict[str, Any]:
        req = state["request"]
        updates: Dict[str, Any] = {"run_id": state.get("run_id") or uuid.uuid4().hex}

        # Validate payload shape.
        product = (req.get("product") or "").strip().upper().replace(" ", "_")
        if product not in PRODUCTS:
            updates["errors"] = [f"product must be one of {PRODUCTS}"]
            updates["next_step"] = "rejected"
            updates["status"] = "REJECTED"
            updates["decision_reason"] = f"Invalid product {product!r}"
            return updates
        currency = (req.get("currency") or "").strip().upper()
        if currency not in SUPPORTED_CURRENCIES:
            updates["errors"] = [f"currency must be one of {SUPPORTED_CURRENCIES}"]
            updates["next_step"] = "rejected"
            updates["status"] = "REJECTED"
            updates["decision_reason"] = f"Invalid currency {currency!r}"
            return updates
        tenor = int(req.get("tenor_days") or 0)
        if tenor not in ALLOWED_TENORS:
            updates["errors"] = [f"tenor_days must be one of {ALLOWED_TENORS}"]
            updates["next_step"] = "rejected"
            updates["status"] = "REJECTED"
            updates["decision_reason"] = f"Invalid tenor {tenor}"
            return updates
        try:
            amount = float(req.get("amount") or 0)
            if amount <= 0:
                raise ValueError()
        except Exception:
            updates["errors"] = ["amount must be a positive number"]
            updates["next_step"] = "rejected"
            updates["status"] = "REJECTED"
            updates["decision_reason"] = "Invalid amount"
            return updates

        # Resolve counterparties via the lookup_company tool. This is a real
        # tool call, not an inline DB query — exactly as a LangChain agent
        # would experience it.
        seller_name = req["seller_name"]
        buyer_name = req["buyer_name"]
        s_lookup = tool_lookup_company(db, name=seller_name)
        b_lookup = tool_lookup_company(db, name=buyer_name)

        updates["tool_calls"] = [
            {"node": "orchestrator", "tool": "lookup_company",
             "args": {"name": seller_name}, "result": s_lookup,
             "timestamp": _dt.datetime.utcnow().isoformat()},
            {"node": "orchestrator", "tool": "lookup_company",
             "args": {"name": buyer_name}, "result": b_lookup,
             "timestamp": _dt.datetime.utcnow().isoformat()},
        ]
        updates["seller_name"] = seller_name
        updates["buyer_name"] = buyer_name
        if s_lookup["found"]:
            updates["seller_id"] = s_lookup["id"]
        else:
            updates["pending_onboard_seller"] = req.get("new_seller")
        if b_lookup["found"]:
            updates["buyer_id"] = b_lookup["id"]
        else:
            updates["pending_onboard_buyer"] = req.get("new_buyer")

        if seller_name.strip().lower() == buyer_name.strip().lower():
            updates["errors"] = ["seller and buyer cannot be the same company"]
            updates["next_step"] = "rejected"
            updates["status"] = "REJECTED"
            updates["decision_reason"] = "Seller and buyer are the same entity."
            return updates

        msg = (
            f"Received invoice intake: {seller_name} → {buyer_name} "
            f"{amount:,.2f} {currency} (tenor {tenor}d, {product})"
        )

        llm_summary = None
        if llm_enabled():
            facts = {
                "seller": {"name": seller_name, "found": s_lookup.get("found", False)},
                "buyer":  {"name": buyer_name,  "found": b_lookup.get("found", False)},
                "amount": amount, "currency": currency, "product": product,
                "tenor_days": tenor, "grace_period_days": req.get("grace_period_days") or 0,
                "has_new_seller_payload": bool(req.get("new_seller")),
                "has_new_buyer_payload":  bool(req.get("new_buyer")),
            }
            rec = safe_structured_call(
                SYSTEM_ORCHESTRATOR,
                "Summarise the intake and recommend the next downstream agent.\n\n"
                + facts_block(facts),
                OrchestratorSummary,
                label="orchestrator_summary",
            )
            if rec is not None:
                llm_summary = rec.summary
                msg = f"{msg}. Orchestrator: {rec.summary}"

        log_event(
            db, agent="orchestrator", action="INVOICE_RECEIVED",
            node="orchestrator", message=msg,
            payload={"seller_name": seller_name, "buyer_name": buyer_name,
                     "amount": amount, "currency": currency, "product": product,
                     "tenor_days": tenor, "llm_summary": llm_summary},
        )
        updates.update(_append_trace(
            "orchestrator", msg, amount=amount, currency=currency, tenor=tenor,
            llm_summary=llm_summary,
        ))
        updates["next_step"] = "onboard_seller"  # always flow through onboarding branch
        return updates
    return orchestrator


# ---------------------------------------------------------------------------
# 2. Onboarding node
# ---------------------------------------------------------------------------

def make_onboarding_node(db: Session) -> Callable[[WonderState], Dict[str, Any]]:
    """Handles seller AND buyer onboarding in a single node for simplicity.
    If either counterparty is missing AND the request included a new_* payload,
    onboard, profile risk, and set limits for them."""

    def onboarding(state: WonderState) -> Dict[str, Any]:
        updates: Dict[str, Any] = {"trace": [], "tool_calls": []}
        req = state["request"]

        def _onboard_side(side: str) -> Optional[int]:
            state_key = f"{side}_id"
            payload_key = f"new_{side}"
            pending = state.get(f"pending_onboard_{side}")
            if state.get(state_key) is not None:
                return state[state_key]
            if pending is None:
                # leave it; downstream node will reject
                return None
            # normalise role suggestion from which side is missing.
            pending = dict(pending)
            pending.setdefault("role", "SELLER" if side == "seller" else "BUYER")
            result = tool_onboard_company(db, **pending)
            updates["tool_calls"].append({
                "node": "onboarding_agent", "tool": "onboard_company",
                "args": pending, "result": result,
                "timestamp": _dt.datetime.utcnow().isoformat(),
            })
            if result.get("created") or result.get("company_id"):
                cid = result["company_id"]
                updates["trace"].append({
                    "node": "onboarding_agent",
                    "timestamp": _dt.datetime.utcnow().isoformat(),
                    "message": f"Onboarded new {side}: {result.get('name') or pending.get('name')} (id={cid}).",
                    "payload": {"side": side, "company_id": cid},
                })
                # Profile risk + set limits right away.
                rp = tool_build_risk_profile(db, company_id=cid)
                updates["tool_calls"].append({
                    "node": "underwriter_agent", "tool": "build_risk_profile",
                    "args": {"company_id": cid}, "result": rp,
                    "timestamp": _dt.datetime.utcnow().isoformat(),
                })
                product = (req.get("product") or "").strip().upper().replace(" ", "_")
                lim = tool_ensure_limits(db, company_id=cid, product=product)
                updates["tool_calls"].append({
                    "node": "credit_limit_agent", "tool": "ensure_limits",
                    "args": {"company_id": cid, "product": product}, "result": lim,
                    "timestamp": _dt.datetime.utcnow().isoformat(),
                })
                # LLM narration of what just happened.
                if llm_enabled():
                    narration = safe_structured_call(
                        SYSTEM_ONBOARDING,
                        "Write a 1-3 short paragraph rationale for the dashboard.\n\n"
                        + facts_block({
                            "onboard_payload": pending,
                            "rating_profile": rp,
                        }),
                        OnboardingSummary,
                        label="onboarding_narration",
                    )
                    if narration is not None:
                        updates["trace"].append({
                            "node": "onboarding_agent",
                            "timestamp": _dt.datetime.utcnow().isoformat(),
                            "message": narration.rationale,
                            "payload": {"side": side, "company_id": cid,
                                        "llm_narration": True},
                        })
                return cid
            return None

        seller_id = _onboard_side("seller")
        buyer_id = _onboard_side("buyer")
        if seller_id is not None:
            updates["seller_id"] = seller_id
        if buyer_id is not None:
            updates["buyer_id"] = buyer_id

        # If still missing → bail early.
        if not updates.get("seller_id", state.get("seller_id")):
            missing = state.get("seller_name")
            updates["next_step"] = "rejected"
            updates["status"] = "REJECTED"
            updates["decision_reason"] = (
                f"Seller '{missing}' is not on the platform and no onboarding payload was provided."
            )
            return updates
        if not updates.get("buyer_id", state.get("buyer_id")):
            missing = state.get("buyer_name")
            updates["next_step"] = "rejected"
            updates["status"] = "REJECTED"
            updates["decision_reason"] = (
                f"Buyer '{missing}' is not on the platform and no onboarding payload was provided."
            )
            return updates

        updates["next_step"] = "create_invoice"
        return updates
    return onboarding


# ---------------------------------------------------------------------------
# 3. Invoice creation (orchestrator side-effect)
# ---------------------------------------------------------------------------

def make_create_invoice_node(db: Session) -> Callable[[WonderState], Dict[str, Any]]:
    def create_invoice(state: WonderState) -> Dict[str, Any]:
        req = state["request"]
        seller_id = state["seller_id"]
        buyer_id = state["buyer_id"]
        currency = req["currency"].strip().upper()
        amount = float(req["amount"])
        amount_usd = round(amount * FX_TO_USD.get(currency, 1.0), 2)
        tenor = int(req["tenor_days"])
        grace = int(req.get("grace_period_days") or 0)
        product = req["product"].strip().upper().replace(" ", "_")

        issue = _dt.datetime.utcnow()
        due = issue + _dt.timedelta(days=tenor)
        invoice_number = req.get("invoice_number") or f"INV-{uuid.uuid4().hex[:10].upper()}"

        invoice = models.Invoice(
            invoice_number=invoice_number,
            seller_id=seller_id, buyer_id=buyer_id,
            product=product, amount=amount, currency=currency, amount_usd=amount_usd,
            tenor_days=tenor, grace_period_days=grace,
            issue_date=issue, due_date=due,
            base_rate=BASE_RATE, credit_spread=0.0,
            status="PENDING",
        )
        db.add(invoice)
        db.flush()

        msg = (
            f"Created invoice {invoice.invoice_number} for ${amount_usd:,.2f} USD "
            f"({amount:,.2f} {currency})"
        )
        log_event(db, agent="orchestrator", action="INVOICE_CREATED",
                  node="orchestrator", invoice_id=invoice.id, message=msg)

        return {
            "invoice_id": invoice.id,
            **_append_trace("orchestrator", msg, invoice_id=invoice.id),
            "next_step": "ensure_limits",
        }
    return create_invoice


# ---------------------------------------------------------------------------
# 4. Credit Limit Agent nodes
# ---------------------------------------------------------------------------

def make_ensure_limits_node(db: Session) -> Callable[[WonderState], Dict[str, Any]]:
    def ensure(state: WonderState) -> Dict[str, Any]:
        invoice = db.query(models.Invoice).get(state["invoice_id"])
        tool_calls: List[Dict[str, Any]] = []
        for side, cid in (("seller", invoice.seller_id), ("buyer", invoice.buyer_id)):
            # Ensure a risk profile exists (underwriter tool).
            rp_existing = db.query(models.RiskProfile).filter(
                models.RiskProfile.company_id == cid
            ).one_or_none()
            if rp_existing is None:
                res = tool_build_risk_profile(db, company_id=cid)
                tool_calls.append({
                    "node": "underwriter_agent", "tool": "build_risk_profile",
                    "args": {"company_id": cid}, "result": res,
                    "timestamp": _dt.datetime.utcnow().isoformat(),
                })
            res = tool_ensure_limits(db, company_id=cid, product=invoice.product)
            tool_calls.append({
                "node": "credit_limit_agent", "tool": "ensure_limits",
                "args": {"company_id": cid, "product": invoice.product}, "result": res,
                "timestamp": _dt.datetime.utcnow().isoformat(),
            })
        return {
            "tool_calls": tool_calls,
            **_append_trace(
                "credit_limit_agent",
                "Ensured risk profiles and credit limits for both counterparties.",
            ),
            "next_step": "find_program",
        }
    return ensure


def make_find_program_node(db: Session) -> Callable[[WonderState], Dict[str, Any]]:
    def find_prog(state: WonderState) -> Dict[str, Any]:
        invoice = db.query(models.Invoice).get(state["invoice_id"])
        res = tool_find_program(
            db, buyer_id=invoice.buyer_id, seller_id=invoice.seller_id,
            product=invoice.product,
        )
        updates: Dict[str, Any] = {
            "tool_calls": [{
                "node": "transaction_agent", "tool": "find_program",
                "args": {"buyer_id": invoice.buyer_id, "seller_id": invoice.seller_id,
                         "product": invoice.product},
                "result": res,
                "timestamp": _dt.datetime.utcnow().isoformat(),
            }],
        }
        if res.get("found"):
            invoice.program_id = res["program_id"]
            db.flush()
            updates["program_id"] = res["program_id"]
            updates["program_headroom_usd"] = res["headroom_usd"]
            updates.update(_append_trace(
                "transaction_agent",
                f"Matched existing program #{res['program_id']}: "
                f"headroom=${res['headroom_usd']:,.2f}",
                program_id=res["program_id"],
            ))
            updates["next_step"] = "limit_check"
        else:
            updates.update(_append_trace(
                "transaction_agent",
                f"No active program for {invoice.seller.name} → {invoice.buyer.name} "
                f"({invoice.product}). Routing to underwriting.",
            ))
            updates["next_step"] = "underwrite_program"
        return updates
    return find_prog


# ---------------------------------------------------------------------------
# 5. Underwriter Agent (new program)
# ---------------------------------------------------------------------------

def make_underwrite_program_node(db: Session) -> Callable[[WonderState], Dict[str, Any]]:
    def underwrite(state: WonderState) -> Dict[str, Any]:
        invoice = db.query(models.Invoice).get(state["invoice_id"])
        requested = max(invoice.amount_usd * 5.0, 1_000_000.0)
        res = tool_decide_new_program(db, invoice_id=invoice.id, requested_limit_usd=requested)
        tc = [{
            "node": "underwriter_agent", "tool": "decide_new_program",
            "args": {"invoice_id": invoice.id, "requested_limit_usd": requested},
            "result": res,
            "timestamp": _dt.datetime.utcnow().isoformat(),
        }]
        if res.get("approved"):
            clamped = res.get("clamped_to_ceiling")
            llm_rationale = res.get("llm_rationale")
            joint = res.get("joint_risk_assessment") or {}
            msg_lines = [
                f"Program approved with ${res['program_limit_usd']:,.0f} bilateral limit."
            ]
            if joint:
                msg_lines.append(
                    f"• Joint risk for {joint['product']}: buyer "
                    f"{joint['buyer_rating']} ({joint['buyer_weight']*100:.0f}% weight), "
                    f"seller {joint['seller_rating']} "
                    f"({joint['seller_weight']*100:.0f}% weight) → "
                    f"combined {joint['combined_rating']}, "
                    f"blended PD {joint['blended_pd_1y']*100:.2f}%."
                )
            if clamped:
                msg_lines.append(
                    f"• Limit clamped to the "
                    f"${PROGRAM_FUNDING_HARD_CEILING_USD:,.0f} platform ceiling."
                )
            if llm_rationale:
                msg_lines.append(f"• Underwriter LLM memo: {llm_rationale}")
            msg = "\n".join(msg_lines)
            return {
                "tool_calls": tc,
                "program_id": res["program_id"],
                **_append_trace(
                    "underwriter_agent", msg,
                    clamped_to_ceiling=clamped,
                    joint_risk_assessment=joint,
                    llm_rationale=llm_rationale,
                ),
                "next_step": "limit_check",
            }
        return {
            "tool_calls": tc,
            **_append_trace(
                "underwriter_agent",
                res.get("reason", "declined"),
                llm_rationale=res.get("llm_rationale"),
            ),
            "next_step": "rejected",
            "status": "REJECTED",
            "decision_reason": res.get("reason"),
        }
    return underwrite


# ---------------------------------------------------------------------------
# 6. Credit Limit Agent — hierarchical headroom check
# ---------------------------------------------------------------------------

def make_limit_check_node(db: Session) -> Callable[[WonderState], Dict[str, Any]]:
    def limit_check(state: WonderState) -> Dict[str, Any]:
        invoice = db.query(models.Invoice).get(state["invoice_id"])
        program = db.query(models.Program).get(invoice.program_id) if invoice.program_id else None
        program_headroom = (
            round(program.credit_limit_usd - program.utilised_usd, 2) if program else 0.0
        )
        buyer_res = tool_hierarchical_headroom(
            db, company_id=invoice.buyer_id, product=invoice.product,
        )
        seller_res = tool_hierarchical_headroom(
            db, company_id=invoice.seller_id, product=invoice.product,
        )
        tc = [
            {"node": "credit_limit_agent", "tool": "hierarchical_headroom",
             "args": {"company_id": invoice.buyer_id, "product": invoice.product},
             "result": buyer_res, "timestamp": _dt.datetime.utcnow().isoformat()},
            {"node": "credit_limit_agent", "tool": "hierarchical_headroom",
             "args": {"company_id": invoice.seller_id, "product": invoice.product},
             "result": seller_res, "timestamp": _dt.datetime.utcnow().isoformat()},
        ]

        amount = invoice.amount_usd
        buyer_head = buyer_res.get("headroom_usd", 0.0)
        seller_head = seller_res.get("headroom_usd", 0.0)
        worst_subtree = min(buyer_head, seller_head)

        updates: Dict[str, Any] = {"tool_calls": tc}
        updates.update(_append_trace(
            "credit_limit_agent",
            (
                f"Headroom check — program=${program_headroom:,.2f}, "
                f"buyer subtree=${buyer_head:,.2f}, "
                f"seller subtree=${seller_head:,.2f}, invoice=${amount:,.2f}"
            ),
            buyer_breakdown=buyer_res.get("breakdown"),
            seller_breakdown=seller_res.get("breakdown"),
            program_headroom=program_headroom,
        ))
        updates["program_headroom_usd"] = program_headroom
        updates["buyer_headroom_usd"] = buyer_head
        updates["seller_headroom_usd"] = seller_head

        def _binding_from(breakdown: Dict[str, float]) -> str:
            """Pick the key with the smallest headroom — that's the binding constraint."""
            if not breakdown:
                return "(no ancestors)"
            return min(breakdown.items(), key=lambda kv: kv[1])[0]

        # Subtree limit breach = hard fail (cannot even go to review).
        if amount > worst_subtree:
            binding_side = "buyer" if buyer_head <= seller_head else "seller"
            binding_head = buyer_head if binding_side == "buyer" else seller_head
            binding_breakdown = (
                buyer_res.get("breakdown", {}) if binding_side == "buyer"
                else seller_res.get("breakdown", {})
            )
            binding_ancestor = _binding_from(binding_breakdown)
            shortfall = amount - binding_head
            used_pct = (
                100.0 * (1.0 - binding_head / (binding_head + amount))
                if (binding_head + amount) > 0 else 100.0
            )
            reason = "\n".join([
                "Rejected — hierarchical credit limit exceeded.",
                (
                    f"• Invoice: ${amount:,.2f} USD "
                    f"({invoice.amount:,.2f} {invoice.currency}), "
                    f"{invoice.product}, tenor {invoice.tenor_days}d."
                ),
                (
                    f"• Buyer subtree headroom: ${buyer_head:,.2f} "
                    f"(binding at {_binding_from(buyer_res.get('breakdown', {}))})."
                ),
                (
                    f"• Seller subtree headroom: ${seller_head:,.2f} "
                    f"(binding at {_binding_from(seller_res.get('breakdown', {}))})."
                ),
                (
                    f"• Binding constraint: {binding_side} side at "
                    f"{binding_ancestor} — only ${binding_head:,.2f} available."
                ),
                (
                    f"• Shortfall: ${shortfall:,.2f} "
                    f"(invoice is {amount/binding_head*100:,.1f}% of the "
                    f"available headroom)."
                    if binding_head > 0
                    else "• The binding limit has no headroom at all."
                ),
            ])
            updates["next_step"] = "rejected"
            updates["status"] = "REJECTED"
            updates["decision_reason"] = reason
            log_event(
                db, agent="credit_limit_agent", action="HARD_LIMIT_BREACH",
                node="credit_limit_agent", severity="DECISION",
                message=reason, invoice_id=invoice.id,
                payload={
                    "invoice_usd": amount,
                    "buyer_headroom_usd": buyer_head,
                    "seller_headroom_usd": seller_head,
                    "binding_side": binding_side,
                    "binding_ancestor": binding_ancestor,
                    "shortfall_usd": shortfall,
                },
            )
            return updates

        # Program limit breach → review.
        if amount > program_headroom:
            overage = amount - program_headroom
            updates["overage_usd"] = overage
            updates["next_step"] = "review"
            updates.update(_append_trace(
                "credit_limit_agent",
                "\n".join([
                    "Program limit exceeded — routing to Review Agent.",
                    (
                        f"• Invoice: ${amount:,.2f} USD  |  "
                        f"Program headroom: ${program_headroom:,.2f}."
                    ),
                    f"• Overage: ${overage:,.2f}.",
                ]),
                overage_usd=overage,
                program_headroom_usd=program_headroom,
            ))
            return updates

        updates["next_step"] = "approve"
        return updates
    return limit_check


# ---------------------------------------------------------------------------
# 7. Review Agent
# ---------------------------------------------------------------------------

def make_review_node(db: Session) -> Callable[[WonderState], Dict[str, Any]]:
    def review(state: WonderState) -> Dict[str, Any]:
        invoice_id = state["invoice_id"]
        overage = float(state["overage_usd"])
        res = tool_decide_overage(db, invoice_id=invoice_id, overage_usd=overage)
        tc = [{
            "node": "review_agent", "tool": "decide_overage",
            "args": {"invoice_id": invoice_id, "overage_usd": overage},
            "result": res, "timestamp": _dt.datetime.utcnow().isoformat(),
        }]
        if res.get("decision") == "TEMP_INCREASE":
            return {
                "tool_calls": tc,
                **_append_trace("review_agent", res["reason"]),
                "next_step": "approve",
            }
        return {
            "tool_calls": tc,
            **_append_trace("review_agent", res["reason"]),
            "next_step": "rejected",
            "status": "REJECTED",
            "decision_reason": res.get("reason"),
        }
    return review


# ---------------------------------------------------------------------------
# 8. Transaction Agent — approve, price, reserve, fund
# ---------------------------------------------------------------------------

def make_approve_node(db: Session) -> Callable[[WonderState], Dict[str, Any]]:
    def approve(state: WonderState) -> Dict[str, Any]:
        invoice_id = state["invoice_id"]
        price_res = tool_price_invoice(db, invoice_id=invoice_id)
        reserve_res = tool_reserve_limits(db, invoice_id=invoice_id)
        tc = [
            {"node": "transaction_agent", "tool": "price_invoice",
             "args": {"invoice_id": invoice_id}, "result": price_res,
             "timestamp": _dt.datetime.utcnow().isoformat()},
            {"node": "credit_limit_agent", "tool": "reserve_limits",
             "args": {"invoice_id": invoice_id}, "result": reserve_res,
             "timestamp": _dt.datetime.utcnow().isoformat()},
        ]
        invoice = db.query(models.Invoice).get(invoice_id)

        # Spec §2 — if the hierarchy invariant was violated during reservation,
        # the reservation already rolled back; reject the invoice.
        if not reserve_res.get("ok"):
            reason = "\n".join([
                "Rejected — reservation would violate hierarchical facility invariant.",
                (
                    f"• Invoice: ${invoice.amount_usd:,.2f} USD "
                    f"({invoice.amount:,.2f} {invoice.currency}), {invoice.product}."
                ),
                f"• Invariant check: {reserve_res.get('error')}.",
                "• Reservation rolled back; no limits were changed.",
            ])
            invoice.status = "REJECTED"
            invoice.decision_reason = reason
            log_event(
                db, agent="credit_limit_agent", action="RESERVATION_FAILED",
                node="credit_limit_agent", severity="DECISION",
                message=reason, invoice_id=invoice.id,
                program_id=invoice.program_id,
            )
            return {
                "tool_calls": tc,
                **_append_trace("credit_limit_agent", reason),
                "status": "REJECTED",
                "decision_reason": reason,
                "next_step": "done",
            }

        invoice.status = "FUNDED"
        invoice.decision_reason = (
            f"Approved at base_rate={price_res['base_rate']:.2%} + "
            f"spread={price_res['credit_spread']:.2%} → "
            f"fee=${price_res['fee_usd']:,.2f}, funded=${price_res['funded_amount_usd']:,.2f}."
        )
        log_event(
            db, agent="transaction_agent", action="FUNDED",
            node="transaction_agent", severity="DECISION",
            message=invoice.decision_reason,
            invoice_id=invoice.id, program_id=invoice.program_id,
        )
        return {
            "tool_calls": tc,
            **_append_trace("transaction_agent", invoice.decision_reason),
            "status": "FUNDED",
            "decision_reason": invoice.decision_reason,
            "fee_usd": price_res["fee_usd"],
            "funded_amount_usd": price_res["funded_amount_usd"],
            "next_step": "done",
        }
    return approve


# ---------------------------------------------------------------------------
# 9. Finalise node (writes back status even on rejection / commits session)
# ---------------------------------------------------------------------------

def make_finalise_node(db: Session) -> Callable[[WonderState], Dict[str, Any]]:
    def finalise(state: WonderState) -> Dict[str, Any]:
        invoice_id = state.get("invoice_id")
        status = state.get("status") or "PENDING"
        reason = state.get("decision_reason")
        if invoice_id:
            invoice = db.query(models.Invoice).get(invoice_id)
            if invoice is not None:
                if invoice.status in ("PENDING",) or status == "REJECTED":
                    invoice.status = status
                if reason:
                    invoice.decision_reason = reason
                log_event(
                    db, agent="orchestrator", action="FLOW_COMPLETE",
                    node="finalise", severity="DECISION",
                    message=f"Final status = {status}",
                    invoice_id=invoice.id,
                )
        db.commit()
        return {**_append_trace("finalise", f"Final status: {status}")}
    return finalise
