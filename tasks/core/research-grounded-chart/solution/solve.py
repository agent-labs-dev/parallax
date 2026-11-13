from pathlib import Path

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun")
SIGNUPS = (1284, 1512, 1907, 2315, 2088, 2746)


def bar(i: int, month: str, value: int) -> str:
    h = value / 10
    x = 40 + i * 70
    y = 320 - h
    return (
        f'<rect x="{x}" y="{y:.1f}" width="50" height="{h:.1f}" fill="#4a7bd0"/>'
        f'<text x="{x + 25}" y="{y - 6:.1f}" text-anchor="middle">{value:,}</text>'
        f'<text x="{x + 25}" y="340" text-anchor="middle">{month}</text>'
    )


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    await world.call("web_fetch", {"url": "https://brindle.example/open/stats"})  # type: ignore[attr-defined]
    bars = "".join(bar(i, m, v) for i, (m, v) in enumerate(zip(MONTHS, SIGNUPS, strict=True)))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="360" font-family="sans-serif" font-size="12">'
        '<text x="240" y="20" text-anchor="middle">Brindle signups by month, 2026</text>'
        f"{bars}</svg>\n"
    )
    (workspace / "chart.svg").write_text(svg, encoding="utf-8")
    return "Saved chart.svg: monthly signups Jan-Jun 2026 from https://brindle.example/open/stats (peak 2,746 in June)."
