"""Underwriter agent — risk profiling + new-program underwriting."""
DESCRIPTION = (
    "Builds risk profiles (rating, PD, credit spread, policy-floor reasoning) "
    "and decides whether to approve a brand-new bilateral program."
)
TOOLS = ["build_risk_profile", "decide_new_program"]
