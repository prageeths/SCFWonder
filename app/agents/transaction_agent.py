"""Transaction agent — routes invoices through approve/review/underwrite and prices fees."""
DESCRIPTION = (
    "Matches invoices to programs, routes them (approve / review / underwrite), "
    "prices fees using base_rate + credit_spread over the tenor window, and marks "
    "the invoice FUNDED."
)
TOOLS = ["find_program", "price_invoice"]
