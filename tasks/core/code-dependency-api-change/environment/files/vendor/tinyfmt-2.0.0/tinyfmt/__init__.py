"""tinyfmt 2.0.0: small display formatters. See CHANGELOG.md for the 1.x -> 2.0 migration."""

__version__ = "2.0.0"

_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥"}


def format_money(amount_cents: int, *, currency: str = "USD") -> str:
    return f"{_SYMBOLS[currency]}{amount_cents / 100:,.2f}"


def format_percent(fraction: float, *, digits: int = 0) -> str:
    return f"{fraction * 100:.{digits}f}%"
