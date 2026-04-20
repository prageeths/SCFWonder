"""Review agent — decides on program-limit overages."""
DESCRIPTION = (
    "Approves a temporary limit increase if (a) both parties are rated BBB or better "
    "OR (b) the overage is within 15% of the program limit. Otherwise denies."
)
TOOLS = ["decide_overage"]
