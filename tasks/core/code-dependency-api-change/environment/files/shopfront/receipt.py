import tinyfmt


def receipt_line(name: str, cents: int, qty: int) -> str:
    return f"{qty} x {name} @ {tinyfmt.money(cents)} = {tinyfmt.money(cents * qty)}"


def discount_note(fraction: float) -> str:
    return f"Saved {tinyfmt.pct(fraction)}"
