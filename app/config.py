"""SCF Wonder — global configuration.

Reads a local `.env` file (if present) so the OpenAI API key is never
baked into source. See `.env.example` for the full list of variables.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # optional: the app still starts without the package installed
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # pragma: no cover
    pass

APP_NAME = "SCF Wonder"
APP_TAGLINE = "LangGraph-powered Supply Chain Finance"

# Paths
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_DB_PATH = DATA_DIR / "wonder.db"
DATABASE_URL = os.environ.get("WONDER_DB_URL", f"sqlite:///{DEFAULT_DB_PATH}")

# Base rate (2% annualised, decimal form).
BASE_RATE = float(os.environ.get("WONDER_BASE_RATE", "0.02"))

# LLM wiring — optional. When OPENAI_API_KEY is absent, every agent
# falls back to its deterministic rule-engine behaviour.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip() or None
LLM_MODEL = os.environ.get("WONDER_LLM_MODEL", "gpt-4o-2024-11-20")
LLM_TEMPERATURE = float(os.environ.get("WONDER_LLM_TEMPERATURE", "0.1"))
LLM_REQUEST_TIMEOUT = float(os.environ.get("WONDER_LLM_TIMEOUT", "30"))


def llm_enabled() -> bool:
    return bool(OPENAI_API_KEY)


# -------- Guardrails (spec §1-§3 in the user request) --------

# Spec §1 — no single Program may be funded above this ceiling.
PROGRAM_MAX_FUNDING_LIMIT_USD = float(
    os.environ.get("WONDER_PROGRAM_MAX_USD", "100_000_000")
)

# A new program's proposed limit is clamped to this ceiling by the
# Underwriter agent. Temporary increases by the Review agent are also
# capped at this ceiling.
PROGRAM_FUNDING_HARD_CEILING_USD = PROGRAM_MAX_FUNDING_LIMIT_USD

# Currencies + FX snapshot (to USD).
SUPPORTED_CURRENCIES = ["USD", "EUR", "GBP", "CAD", "MXN", "BRL", "COP", "JPY"]
FX_TO_USD = {
    "USD": 1.00,
    "EUR": 1.08,
    "GBP": 1.27,
    "CAD": 0.74,
    "MXN": 0.058,
    "BRL": 0.20,
    "COP": 0.00025,
    "JPY": 0.0067,
}

# Allowed tenors (days)
ALLOWED_TENORS = [30, 60, 90]

# Products
PRODUCT_FACTORING = "FACTORING"
PRODUCT_REVERSE_FACTORING = "REVERSE_FACTORING"
PRODUCTS = [PRODUCT_FACTORING, PRODUCT_REVERSE_FACTORING]

# Rating ladder (best -> worst). Shared by Underwriter + Credit Limit agents.
RATING_LADDER = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC"]
RATING_BANDS = {
    # rating -> (max_pd_1y, base_spread)
    "AAA": (0.0010, 0.0050),
    "AA":  (0.0030, 0.0080),
    "A":   (0.0070, 0.0120),
    "BBB": (0.0150, 0.0180),
    "BB":  (0.0350, 0.0260),
    "B":   (0.0700, 0.0380),
    "CCC": (1.0000, 0.0600),
}

# Named-major floors (case-insensitive substring match against the company or
# any ancestor in its hierarchy).
NAMED_MAJORS_AAA = {"Walmart", "Amazon", "Coca-Cola"}
NAMED_MAJORS_AA = {
    "Target", "Kroger", "Albertsons", "Jewel-Osco", "Costco",
    "Best Buy", "CVS", "Walgreens", "Publix", "PepsiCo", "Pepsi",
}

THRESHOLD_AAA_REVENUE = 250_000_000_000.0
THRESHOLD_AA_REVENUE = 100_000_000_000.0
THRESHOLD_B_MAX_REVENUE = 5_000_000.0

INDUSTRIES = [
    "Food & Beverage", "Retail", "Pharmaceuticals", "Consumer Electronics",
    "Apparel", "Home Goods", "Logistics", "Packaging", "Ingredients",
    "Watches & Accessories", "Industrial",
]
INDUSTRY_RISK = {
    "Food & Beverage": 0.002, "Retail": 0.003, "Pharmaceuticals": 0.0015,
    "Consumer Electronics": 0.004, "Apparel": 0.005, "Home Goods": 0.0035,
    "Logistics": 0.004, "Packaging": 0.003, "Ingredients": 0.0035,
    "Watches & Accessories": 0.0045, "Industrial": 0.005,
}
COUNTRIES = [
    ("US", "United States"), ("CA", "Canada"), ("MX", "Mexico"),
    ("BR", "Brazil"), ("CO", "Colombia"), ("GB", "United Kingdom"),
    ("DE", "Germany"), ("FR", "France"), ("JP", "Japan"),
]
COUNTRY_RISK = {
    "US": 0.0005, "CA": 0.0008, "MX": 0.0040, "BR": 0.0060, "CO": 0.0070,
    "GB": 0.0010, "DE": 0.0010, "FR": 0.0015, "JP": 0.0010,
}
