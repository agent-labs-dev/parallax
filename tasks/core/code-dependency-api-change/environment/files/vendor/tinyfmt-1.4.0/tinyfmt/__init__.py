"""tinyfmt 1.4.0: small display formatters."""

__version__ = "1.4.0"


def money(cents: int, symbol: str = "$") -> str:
    return f"{symbol}{cents / 100:,.2f}"


def pct(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"
