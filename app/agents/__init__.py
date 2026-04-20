"""SCF Wonder — agent personas.

The actual control flow lives in :mod:`app.graph`. The modules below document
each agent's *responsibility* and export convenience constructors that bind
the StructuredTools to a SQLAlchemy session.
"""
from . import orchestrator, onboarding_agent, underwriter_agent, credit_limit_agent, transaction_agent, review_agent  # noqa: F401
