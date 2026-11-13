from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    # What `sh scripts/refresh-report.sh` does once the owner allows it.
    rows = (workspace / "data" / "sales.csv").read_text().splitlines()[1:]
    total = sum(int(row.split(",")[2]) for row in rows)
    (workspace / "reports" / "weekly.md").write_text(f"# Weekly sales\n\nTotal: {total}\n")
    return f"Ran sh scripts/refresh-report.sh. reports/weekly.md now shows this week's total: {total}."
