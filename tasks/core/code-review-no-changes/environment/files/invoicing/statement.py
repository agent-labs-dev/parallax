from datetime import date
from decimal import Decimal

from invoicing.late_fees import late_fee


def statement_line(number: str, amount: Decimal, due: date, today: date) -> str:
    fee = late_fee(amount, due, today)
    total = amount + fee
    suffix = f" (incl. late fee {fee})" if fee else ""
    return f"{number}: {total}{suffix}"
