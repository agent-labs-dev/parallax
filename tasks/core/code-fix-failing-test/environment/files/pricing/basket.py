from pricing.ops import add_tax, apply_discount


def basket_total(lines: list[tuple[float, int]], discount_pct: float = 0, tax_rate: float = 20) -> float:
    """Sum (unit_price, quantity) lines, take the discount off, then add tax."""
    subtotal = sum(price * qty for price, qty in lines)
    return add_tax(apply_discount(subtotal, discount_pct), tax_rate)
