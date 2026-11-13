from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    limits = workspace / "budget" / "limits.py"
    src = limits.read_text(encoding="utf-8")
    src = src.replace(
        '    """How much of the monthly budget is left after spent."""\n    return MONTHLY_BUDGET - spent\n',
        '    """How much of the monthly budget is left after spent, never below zero."""\n'
        "    return max(MONTHLY_BUDGET - spent, 0)\n",
    )
    limits.write_text(src, encoding="utf-8")
    return "Updated remaining() in budget/limits.py to your version (line numbers left out); it now floors at 0."
