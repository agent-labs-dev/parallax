import csv
from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    with (workspace / "journeys-by-type.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    modes = [c for c in rows[0] if c.endswith("journeys (m)")]

    def year_total(fy: str, mode: str) -> float:
        return sum(float(r[mode]) for r in rows if r["Period and financial year"].endswith(fy))

    growth = {m: (year_total("25/26", m) / year_total("24/25", m) - 1) * 100 for m in modes}
    best = max(growth, key=lambda m: growth[m])
    name = best.removesuffix(" journeys (m)")
    return (
        f"{name} grew the most in 2025/26, the last full financial year: up {growth[best]:.1f}% on 2024/25. "
        "2026/27 only has two periods so far, so I left it out."
    )
