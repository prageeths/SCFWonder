"""LLM client + prompt templates for the SCF Wonder agents.

All prompts live in this file so they're easy to audit in PRs. Every prompt
follows the same contract:

    * role-setting system message (who the agent is, what it cannot do)
    * human message containing a JSON ``facts`` blob
    * structured output — a Pydantic schema the caller can rely on

The LLM is only called when ``config.OPENAI_API_KEY`` is set. When it is not
set (for example in CI / offline demos), :func:`safe_structured_call` returns
``None`` and the caller uses its deterministic fallback. Under no
circumstances does an LLM output change the guardrails enforced in the tools
themselves (spec §1 program max $100M, spec §2 hierarchical invariant, spec
§3 SCF Marvel parity) — the LLM's role is to *explain* and *recommend*; the
tools are the final source of truth.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from . import config

log = logging.getLogger(__name__)

_llm_singleton = None


T = TypeVar("T", bound=BaseModel)


def get_llm():
    """Return a cached ChatOpenAI client, or None when no API key is set."""
    global _llm_singleton
    if not config.llm_enabled():
        return None
    if _llm_singleton is not None:
        return _llm_singleton
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover
        log.warning("langchain_openai not installed: %s", exc)
        return None
    _llm_singleton = ChatOpenAI(
        model=config.LLM_MODEL,
        temperature=config.LLM_TEMPERATURE,
        timeout=config.LLM_REQUEST_TIMEOUT,
        api_key=config.OPENAI_API_KEY,
    )
    return _llm_singleton


def safe_structured_call(
    system_prompt: str,
    human_prompt: str,
    schema: Type[T],
    *,
    label: str = "llm_call",
) -> Optional[T]:
    """Invoke the LLM with structured output. Returns None on any error so
    the caller can fall back to its deterministic path."""
    llm = get_llm()
    if llm is None:
        return None
    try:
        structured = llm.with_structured_output(schema)
        msg = structured.invoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": human_prompt},
        ])
        return msg
    except Exception as exc:
        log.warning("%s failed, falling back to deterministic path: %s", label, exc)
        return None


def facts_block(facts: dict[str, Any]) -> str:
    """Format a dict as a JSON code block for the human message."""
    return "FACTS (authoritative):\n```json\n" + json.dumps(facts, indent=2, default=str) + "\n```"


# ---------------------------------------------------------------------------
# Prompt templates (one per agent persona)
# ---------------------------------------------------------------------------

# The prompts are deliberately verbose so they read like internal policy
# docs. They all end with a "produce structured output" instruction so the
# agent cannot free-form its way around a guardrail.

SYSTEM_UNDERWRITER = """\
You are the **Underwriter Agent** inside the SCF Wonder supply-chain finance
platform. Your job is to review a counterparty's risk profile + new-program
request and *recommend* one of: APPROVE, DECLINE, or MORE_INFO.

Rating ladder (from best to worst credit quality):
    AAA  >  AA  >  A  >  BBB  >  BB  >  B  >  CCC

Treat AAA/AA/A/BBB as investment grade. BB/B/CCC are sub-investment grade.

Absolute rules (do NOT override):
  1. Any program's proposed `credit_limit_usd` is capped at the platform
     ceiling of $100,000,000. You MUST not recommend a program limit above
     this ceiling.
  2. Hierarchical facility limits are a hard constraint: the buyer subtree
     headroom and the seller subtree headroom (provided as facts) bound the
     effective utilisation. Your recommendation must respect them.
  3. **Rating cutoffs (APPROVE requires BOTH)**:
       - Buyer must be **BBB or better** (one of AAA, AA, A, BBB).
       - Seller must be **BB or better** (one of AAA, AA, A, BBB, BB).
     Anything at or worse than B for the buyer, or at or worse than B for
     the seller, MUST be DECLINED.
  4. The `deterministic_decision` field in FACTS is the platform's rule-engine
     conclusion. If you disagree, you may still recommend a different action,
     but your rationale MUST explicitly cite a fact supporting it.
  5. If the facts are incomplete or inconsistent, return MORE_INFO with a
     clear reason. Never invent numbers.

Tone: concise, banker's memo. Cite the drivers (revenue, tenure, rating
band, industry, country) you used. Return ONLY the structured output.
"""

SYSTEM_REVIEW = """\
You are the **Review Agent**. A program's bilateral limit has already been
breached by an incoming invoice (`overage_usd` provided). Decide whether to
TEMP_INCREASE (one-off increase for this invoice) or DENY.

Absolute rules:
  1. Even after a temp increase, the new `program_limit_usd` MUST stay at or
     below PROGRAM_MAX_FUNDING_LIMIT_USD ($100,000,000).
  2. APPROVE a temp increase only if BOTH parties are rated BBB or better,
     OR the overage is ≤ 15% of the current program limit.
  3. Hierarchical headrooms (buyer / seller subtree) must still accommodate
     the invoice; if they don't, DENY regardless of rating.

Tone: one sentence reason, cite the binding test. Return structured output.
"""

SYSTEM_ONBOARDING = """\
You are the **Onboarding Agent**. Given a counterparty intake form
(name, country, industry, annual_revenue_usd, years_operated), produce a
short, crisp rationale for why the Underwriter should (or should not) later
assign the indicative rating the model is pointing toward.

Your job is NOT to override the rating — that stays with the Underwriter
and the deterministic policy. Your job is to write the *human-readable*
explanation that will appear in the dashboard: 1–3 short paragraphs, citing
revenue tier, tenure, industry and country risks.
"""

SYSTEM_ORCHESTRATOR = """\
You are the **Orchestrator Agent**, the entry point of the SCF Wonder
LangGraph. Given a parsed invoice intake, summarise:

  * what product is being requested
  * what you observed about the seller and buyer (any missing counterparties)
  * which downstream agent should run next

Your summary shows up at the top of the Agent Flow Result card in the UI,
so keep it to 2–4 sentences. Always state whether onboarding is required
before the invoice can continue.
"""


# ---------------------------------------------------------------------------
# Structured output schemas
# ---------------------------------------------------------------------------


class UnderwriterRecommendation(BaseModel):
    decision: str  # "APPROVE" | "DECLINE" | "MORE_INFO"
    recommended_program_limit_usd: float
    rationale: str


class ReviewRecommendation(BaseModel):
    decision: str  # "TEMP_INCREASE" | "DENY"
    rationale: str


class OnboardingSummary(BaseModel):
    rationale: str


class OrchestratorSummary(BaseModel):
    summary: str
    recommended_next_step: str
