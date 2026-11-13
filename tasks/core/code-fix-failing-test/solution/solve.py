from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    ops = workspace / "pricing" / "ops.py"
    ops.write_text(ops.read_text().replace("round(price * pct / 100, 2)", "round(price * (1 - pct / 100), 2)"))
    return (
        "apply_discount in pricing/ops.py returned the discount amount (price * pct / 100) instead of the "
        "discounted price. I changed it to price * (1 - pct / 100); all 5 tests pass now."
    )
