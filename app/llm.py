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

SYSTEM_RATING_ANALYST = """\
You are the **Rating Analyst** inside the SCF Wonder supply-chain finance
platform. Given a single company's full context, determine:

  * the final credit rating (one of: AAA, AA, A, BBB, BB, B, CCC — best to worst),
  * the 1-year probability of default as a decimal (0.0002 … 0.20),
  * the credit spread as a decimal (0.0025 … 0.06),
  * a 2–3 sentence rationale citing the specific drivers you used.

Policy you MUST respect:
  * Annual revenue ≥ $250B (anywhere in the corporate tree) → floor AAA.
  * Annual revenue ≥ $100B (anywhere in the corporate tree) → floor AA.
  * Named majors (Walmart / Amazon / Coca-Cola) → floor AAA.
  * Named majors (Target / Kroger / Albertsons / Jewel-Osco / Costco /
    Best Buy / CVS / Walgreens / Publix / PepsiCo) → floor AA.
  * Own annual revenue < $5M → exactly B (neither better nor worse).
  * Own operating history < 2 years → cap at BB.
  * Own operating history < 5 years → cap at A.

Drivers you should weigh:
  * Revenue tier (size = survival capacity).
  * Tenure (track record reduces PD).
  * Industry and country risk (benchmarks are in the facts).
  * Parent chain strength (a subsidiary of a $600B parent inherits some
    strength but not enough to leapfrog the floors above).
  * Observed payment history (paid_pct, settled_pct, rejected_pct).

Typical PD bands:
  AAA ≤ 0.10%   AA ≤ 0.30%   A ≤ 0.70%   BBB ≤ 1.50%
  BB  ≤ 3.50%   B  ≤ 7.00%   CCC otherwise.

Typical spread bands (same order): 0.50% · 0.80% · 1.20% · 1.80% · 2.60%
· 3.80% · 6.00%. Pick values consistent with the rating you assign.

Return ONLY the structured output.
"""


SYSTEM_UNDERWRITER = """\
You are the **Underwriter Agent** inside the SCF Wonder supply-chain finance
platform. You are evaluating whether to open a brand-new bilateral program
between a specific buyer and seller, and — if yes — how large a program
limit is prudent.

Rating ladder (best → worst): AAA > AA > A > BBB > BB > B > CCC

Policy (inviolable):
  1. Hard platform ceiling: `recommended_program_limit_usd` ≤ $100,000,000.
  2. Hard rating cutoffs: APPROVE only if buyer ≤ BBB and seller ≤ BB.
     Any single fail ⇒ DECLINE.
  3. Hierarchical facility headroom is authoritative. The program limit
     you recommend must not exceed **either** the buyer subtree headroom
     **or** the seller subtree headroom.

How to size the limit when you APPROVE:
  * Start from the invoice amount × 5 as a floor (you want reasonable
    headroom for repeat business).
  * Consider **both parties**:
      - The buyer's combined credit-limit headroom (GLOBAL + product).
      - The seller's combined credit-limit headroom.
      - Each party's existing book of programs (total limits as buyer,
        total limits as seller). A buyer already concentrated in 20
        programs at high utilisation warrants a smaller new program.
      - Observed payment history ratios (paid_pct, settled_pct,
        rejected_pct) as a proxy for behavioural risk.
  * For strong IG names with clean histories, you MAY go up to the
    $100M ceiling. For newly onboarded firms with limited data, size
    modestly (roughly 5–10× invoice amount, or $500k–$5M).
  * For product FACTORING, the buyer's credit carries 70% of the risk
    weight; for REVERSE_FACTORING, 75%. Tighten the limit when the
    dominant-risk side is weaker.

Output contract:
  * `decision` ∈ {"APPROVE", "DECLINE", "MORE_INFO"}.
  * `recommended_program_limit_usd` — 0 if DECLINE, else your prudent
    sizing in USD (≤ $100M).
  * `rationale` — 2–4 sentences, banker-memo tone. Cite specific numbers
    (e.g. "buyer AAA, seller AA, buyer subtree headroom $1.2B, invoice
    $120k, sized at $5M = ~42× invoice for repeat business").

Return ONLY the structured output.
"""

SYSTEM_REVIEW = """\
You are the **Review Agent**. A program's bilateral limit has been breached
by an incoming invoice (`overage_usd` provided). Decide whether to
TEMP_INCREASE (one-off increase for this invoice) or DENY, and if
TEMP_INCREASE, by how much.

Policy (inviolable):
  1. After any temp increase, the new program limit MUST stay at or below
     the $100,000,000 platform ceiling.
  2. Hierarchical headrooms (buyer / seller subtree) must still fully
     accommodate the invoice; if they don't, DENY regardless of ratings.

Guidance for APPROVE:
  * APPROVE readily when BOTH parties are BBB or better AND the overage is
    ≤ 25% of the current program limit.
  * APPROVE cautiously when overage ≤ 15% of the program limit (regardless
    of rating).
  * For programs with good payment track records (settled_pct ≥ 90% over
    the last 25 invoices) you MAY approve a slightly larger lift.
  * Consider the buyer's overall book (count_as_buyer, total_limit_usd_as_buyer)
    — a buyer already at heavy overall utilisation warrants a tighter lift.

Guidance for DENY:
  * Any rating at B or CCC on either side + overage > 15%: DENY.
  * Overage > 35% of program limit without a strong IG pair: DENY.

Output contract:
  * `decision` ∈ {"TEMP_INCREASE", "DENY"}.
  * `temp_increase_amount_usd` — 0 when DENY, else a value ≤ overage_usd
    (you may approve the exact overage or a slightly larger buffer up to
    ceiling constraints).
  * `rationale` — 1–2 sentences citing specific numbers.

Return ONLY the structured output.
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


class RatingAnalystResult(BaseModel):
    rating: str                   # "AAA" | "AA" | ... | "CCC"
    pd_1y: float                  # decimal, e.g. 0.0032
    credit_spread: float          # decimal, e.g. 0.0080
    rationale: str


class UnderwriterRecommendation(BaseModel):
    decision: str                 # "APPROVE" | "DECLINE" | "MORE_INFO"
    recommended_program_limit_usd: float
    rationale: str


class ReviewRecommendation(BaseModel):
    decision: str                 # "TEMP_INCREASE" | "DENY"
    temp_increase_amount_usd: float = 0.0
    rationale: str


class OnboardingSummary(BaseModel):
    rationale: str


class OrchestratorSummary(BaseModel):
    summary: str
    recommended_next_step: str
