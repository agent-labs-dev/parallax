"""Late fees for overdue invoices."""

from datetime import date
from decimal import Decimal

LATE_FEE_PERCENT = Decimal("2")
GRACE_DAYS = 14


def days_overdue(due: date, today: date) -> int:
    """Whole days since the due date; 0 or less means not yet due."""
    return (due - today).days


def late_fee(amount: Decimal, due: date, today: date) -> Decimal:
    """The fee to add once an invoice is more than GRACE_DAYS past due."""
    if days_overdue(due, today) <= GRACE_DAYS:
        return Decimal("0.00")
    return (amount * LATE_FEE_PERCENT).quantize(Decimal("0.01"))
