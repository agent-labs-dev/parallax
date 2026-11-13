from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    return (
        "Two bugs, I'd hold the merge:\n\n"
        "1. days_overdue has the subtraction backwards: (due - today).days is negative once the invoice is "
        "overdue, so late_fee never charges anything. It should be (today - due).days.\n"
        "2. late_fee multiplies by LATE_FEE_PERCENT (2) directly, so the fee is 200% of the amount rather "
        "than 2%. It needs amount * LATE_FEE_PERCENT / 100.\n\n"
        "The one test only covers a not-yet-due invoice, which is why it still passes. I haven't changed any files."
    )
