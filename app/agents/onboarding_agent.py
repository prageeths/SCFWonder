"""Onboarding agent — creates any missing counterparty and bootstraps it.

Pipeline (tools it invokes in order):
    onboard_company → build_risk_profile → ensure_limits
"""
DESCRIPTION = "Onboards new buyers/sellers, then hands off to underwriting & credit limit agents."
TOOLS = ["onboard_company", "build_risk_profile", "ensure_limits"]
