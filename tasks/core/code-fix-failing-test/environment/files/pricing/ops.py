"""Price arithmetic shared by the basket and the invoice renderer."""


def apply_discount(price: float, pct: float) -> float:
    """Return the price after taking pct percent off."""
    return round(price * pct / 100, 2)


def add_tax(price: float, rate: float) -> float:
    """Return the price with rate percent tax added."""
    return round(price * (1 + rate / 100), 2)
