# SCF Wonder — LangGraph-powered Supply Chain Finance

An AI-Native supply chain finance platform supporting **Factoring** and
**Reverse Factoring**. Unlike a traditional rules-engine implementation, every
invoice here is processed by a **LangGraph** state machine that coordinates
six cooperating agents, each invoking typed **LangChain StructuredTools**.

## Agent topology

```
orchestrator
    │
    ▼
onboarding_agent ─── (missing + no payload) ──► finalise
    │
    ▼
create_invoice ──► ensure_limits ──► find_program
                                        │
                         (no program) ──┴── (match)
                                │           │
                                ▼           ▼
                      underwrite_program   limit_check
                        │       │           │    │    │
                      (decl) (appr)       hard  rev  ok
                        │       │           │    │    │
                        ▼       ▼           ▼    ▼    ▼
                     finalise limit_check finalise review approve
                                                    │      │
                                                    ▼      ▼
                                                 approve  finalise
                                                    │
                                                    ▼
                                                 finalise
```

Each agent is a LangGraph node that invokes LangChain `StructuredTool`s with
Pydantic schemas:

| Agent | LangGraph Node | StructuredTools |
|---|---|---|
| Orchestrator | `orchestrator`, `create_invoice`, `finalise` | `lookup_company` |
| Onboarding Agent | `onboarding_agent` | `onboard_company`, `build_risk_profile`, `ensure_limits` |
| Underwriter Agent | `underwrite_program` | `build_risk_profile`, `decide_new_program` |
| Credit Limit Agent | `ensure_limits`, `limit_check`, `approve` | `ensure_limits`, `hierarchical_headroom`, `reserve_limits` |
| Transaction Agent | `find_program`, `approve` | `find_program`, `price_invoice` |
| Review Agent | `review` | `decide_overage` |

Every tool call is logged both to the database (`agent_events` table) and
into the graph state (`tool_calls` list), so the UI renders a complete
audit trail of *every* decision.

## Features (matches the SCF Marvel spec)

- Hierarchical companies (Walmart Global → US / LATAM → Brazil / Colombia / …),
  Target regions, Kroger banners, Albertsons / Jewel-Osco, Costco, Best Buy,
  CVS, Walgreens, Amazon / Whole Foods, Publix, Wegmans, Trader Joe's, Aldi,
  H-E-B, Meijer and more.
- **520+ US-based sellers**: Kellogg, Quaker, Coca-Cola, PepsiCo, Mondelez,
  General Mills, Hershey, Mars, Reckitt/Delsym, Pfizer/Advil, Thermos, HP,
  Dell, Bose, Garmin, Fossil, Movado, Citizen, Bulova, Skagen, Levi's,
  Hanesbrands, plus tier-2 packaging / ingredients / logistics (Cargill,
  Berry Global, WestRock, ADM, XPO …).
- ~1,560 buyer ↔ seller programs with realistic limits and ~12,000 invoices
  over the last 2 years across 8 currencies (USD / EUR / GBP / CAD / MXN /
  BRL / COP / JPY) with FX normalisation.
- **Hierarchical credit limits**: each company has its own GLOBAL + per-product
  (Factoring / Reverse Factoring) limits; subtree utilisation is bounded by
  every ancestor.
- **Pricing**: `fee = amount_usd × (base_rate + credit_spread) ×
  (tenor + grace) / 360`. Base rate (2% default) shown in the top bar.
- **Rating policy**: AAA for Walmart / Amazon / Coca-Cola or ≥ $250B revenue;
  AA for Target / Kroger / Jewel-Osco / Costco / Best Buy / CVS / Walgreens /
  PepsiCo / Publix or ≥ $100B; B-pin for <$5M; new-entrant caps (< 2y → BB,
  < 5y → A).
- **Facility explainability**: `GET /api/programs/{id}/facility` returns a
  five-step trace showing per-currency invoice aggregation → program limit →
  buyer subtree → seller subtree → binding constraint.
- **Dashboard** with six tabs: Dashboard, New Invoice, Transactions, Agent Graph,
  Agent Console, Programs, Companies.

## Project layout

```
.
├── app/
│   ├── agents/                 # Agent persona documentation
│   ├── tools/                  # LangChain StructuredTools (DB-bound)
│   │   ├── company_tools.py
│   │   ├── credit_limit_tools.py
│   │   ├── review_tools.py
│   │   ├── transaction_tools.py
│   │   └── underwriting_tools.py
│   ├── graph/                  # LangGraph state machine
│   │   ├── state.py            # WonderState TypedDict
│   │   ├── nodes.py            # Per-node callables
│   │   └── graph.py            # StateGraph assembly + runner
│   ├── config.py
│   ├── database.py
│   ├── main.py                 # FastAPI routes
│   ├── models.py               # SQLAlchemy models
│   └── schemas.py              # API Pydantic schemas
├── scripts/
│   └── seed.py                 # Generates companies / programs / invoices
├── web/
│   ├── templates/index.html    # Single-page dashboard
│   └── static/{app.js,styles.css}
├── data/                       # SQLite DB lives here (gitignored)
├── requirements.txt
└── README.md
```

## Running locally

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Seed (~572 companies, ~1,560 programs, 12,000 invoices)
python -m scripts.seed        # or: python -m scripts.seed 25000

# 3. Start
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 4. Open http://localhost:8000/
```

## API highlights

| Endpoint | Description |
| --- | --- |
| `GET  /api/meta` | Base rate, currencies, tenors, countries, industries, rating ladder, FX snapshot |
| `POST /api/invoices` | Runs the LangGraph. Returns the invoice, the full graph trace, every tool call, and persisted events |
| `GET  /api/invoices/{id}` | Invoice + its agent events |
| `POST /api/companies` | Manual onboarding (runs `onboard_company` → `build_risk_profile` → `ensure_limits` tools) |
| `GET  /api/companies/exists` | Live recognition check used by the invoice form |
| `GET  /api/companies` | Filter / sort by rating / revenue / role / name |
| `GET  /api/companies/{id}` | Hierarchy + credit limits + risk profile + programs |
| `GET  /api/programs/{id}/facility` | Step-by-step facility-limit explainability |
| `GET  /api/events` | Agent activity console (filter by agent / node / severity) |
| `GET  /api/summary` | Dashboard totals and recent decisions |
| `GET  /api/transactions/summary` | Transactions tab roll-ups |

## Inspecting a run

After submitting an invoice, the **Agent Flow Result** card shows three inner tabs:

1. **Graph Trace** — chronological list of node messages (`[orchestrator]`,
   `[onboarding_agent]`, `[credit_limit_agent]`, `[underwriter_agent]`,
   `[transaction_agent]`, `[review_agent]`, `[finalise]`).
2. **Tool Calls** — every LangChain `StructuredTool.invoke(...)` call with its
   `args` (Pydantic-validated) and its `result` JSON.
3. **Persisted Events** — the rows written to the `agent_events` table, with
   agent + node tags for filtering.

This is *agentic observability* end-to-end.

## Notes

SCF Wonder mirrors the domain of SCF Marvel (the sibling repo) but is a fresh
codebase structured around agentic patterns:

- The graph replaces the straight-line `OrchestrationAgent.submit_invoice`
  call with a real LangGraph runtime that supports conditional edges,
  fan-out nodes, and state reducers.
- Each skill is a self-contained `StructuredTool` with a Pydantic schema — so
  plugging a real LLM in front of the graph (for example to let an LLM decide
  which tool to call at a given branch) is a one-file change.
- The `WonderState` TypedDict has append-only reducers (`operator.add`) for
  `trace`, `tool_calls` and `errors`, giving you a perfect audit log of the
  run without manual plumbing.

Synthetic data only — no live credit decisions.
