"""Monthly budget arithmetic for the team expenses dashboard."""

# Pence. Finance resets this on the first of each month.
MONTHLY_BUDGET = 150_000

WARN_AT = 0.8


def remaining(spent: int) -> int:
    """How much of the monthly budget is left after spent."""
    return MONTHLY_BUDGET - spent


def needs_warning(spent: int) -> bool:
    """True once spending passes the warning threshold."""
    return spent >= MONTHLY_BUDGET * WARN_AT
