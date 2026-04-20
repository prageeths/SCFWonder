"""Orchestrator agent — validates the intake and coordinates the other agents.

Implemented as LangGraph nodes in :mod:`app.graph.nodes`. This file documents
the contract.

Responsibilities:
    * Validate product / currency / tenor / amount on the payload.
    * Resolve seller and buyer via the ``lookup_company`` tool.
    * Hand off to the onboarding agent when a counterparty is missing.
    * Create the Invoice row once both parties are resolved.
    * Write the final status / decision reason after the flow completes.
"""
DESCRIPTION = "Validates intake, resolves counterparties, coordinates the agent team."
TOOLS = ["lookup_company"]
