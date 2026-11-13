from pathlib import Path

BRIEF = """# Street trees and summer heat: board brief

**Decision sought:** approve targeted street-tree planting in the six lowest-canopy wards (option A).

## What the research found
- Streets with over 30% tree canopy were **2.4°C cooler** at 3pm on hot days than streets under 10%.
- Heat-related ambulance call-outs were **17% higher** on days of 30°C or more than on days below 25°C.
- Planting 4,800 trees in the six lowest-canopy wards would cost **£3.1m over five years**, then about £140,000 a year.

## Why it matters
Summer 2025 had 14 days of 30°C or more, against a ten-year average of 6. The six target wards
hold 38% of the city's residents aged over 75.

## Options
- A. Targeted planting: £3.1m over five years (recommended).
- B. City-wide planting: £9.4m over five years; annual maintenance has not been estimated.
- C. No new planting; rely on cooling centres.

## Caveats
One unusually hot summer of data; street air temperature rather than indoor temperature; call-outs
are city-wide, not tied to streets; trees take 8-12 years to deliver the full cooling; pavement works
on the narrowest streets are not yet costed.

Source: Harbourton City Council Environment Directorate research packet, May 2026.
"""


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    (workspace / "brief.md").write_text(BRIEF, encoding="utf-8")
    return "Saved a one-page brief to brief.md, leading with the three headline figures and the recommendation."
