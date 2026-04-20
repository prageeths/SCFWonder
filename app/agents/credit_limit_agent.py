"""Credit Limit agent — owns all hierarchical limit arithmetic."""
DESCRIPTION = (
    "Sets and enforces hierarchical credit limits. Walks the buyer AND seller "
    "subtrees, computes the tightest available headroom, and reserves amounts "
    "against program + company limits after an approval."
)
TOOLS = ["ensure_limits", "hierarchical_headroom", "reserve_limits"]
