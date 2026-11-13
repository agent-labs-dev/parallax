import csv
from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    # June arrives as a turn-02 attachment; turn-01 is scoped to data/ as the user asked.
    sources = sorted((workspace / "data").glob("sales-*.csv"))
    if step == "turn-02":
        sources += sorted((workspace / "attachments").glob("sales-*.csv"))
    totals: list[tuple[str, float]] = []
    for path in sources:
        with path.open(newline="", encoding="utf-8") as f:
            total = sum(float(r["amount"]) for r in csv.DictReader(f))
        totals.append((path.stem.removeprefix("sales-"), round(total, 2)))
    out = workspace / "reports" / "monthly-summary.csv"
    out.parent.mkdir(exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["month", "total_sales_gbp"])
        writer.writerows((month, f"{total:.2f}") for month, total in totals)
    summary = ", ".join(f"{month}: £{total:,.2f}" for month, total in totals)
    if step == "turn-01":
        return f"Written to reports/monthly-summary.csv - {summary}."
    return f"Re-ran it with the June export and overwrote reports/monthly-summary.csv - {summary}."
