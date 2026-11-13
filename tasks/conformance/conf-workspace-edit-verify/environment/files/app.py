def apply_discount(price: float, percent: float) -> float:
    """Price after taking `percent` percent off, e.g. apply_discount(100, 10) == 90."""
    return price - price * percent
