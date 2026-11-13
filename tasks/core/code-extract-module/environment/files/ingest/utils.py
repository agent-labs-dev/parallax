"""Parsing helpers for the bank-statement importer."""

from datetime import date, datetime
from decimal import Decimal


def parse_date(text: str) -> date:
    """Statement dates are day-first: 03/06/2026."""
    return datetime.strptime(text.strip(), "%d/%m/%Y").date()


def parse_amount(text: str) -> Decimal:
    """'1,234.50' -> Decimal('1234.50'); a trailing CR marks a credit, which is negative."""
    cleaned = text.strip().replace(",", "")
    if cleaned.endswith("CR"):
        return -Decimal(cleaned.removesuffix("CR").strip())
    return Decimal(cleaned)
