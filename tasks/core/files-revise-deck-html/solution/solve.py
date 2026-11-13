from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    deck = workspace / "deck.html"
    html = deck.read_text(encoding="utf-8")
    html = html.replace("<h2>Momentum is real—and concentrated</h2>", "<h2>Q2 results</h2>", 1)
    html = html.replace("<strong>42%</strong>activation rate", "<strong>44%</strong>activation rate", 1)
    deck.write_text(html, encoding="utf-8")
    return 'Updated slide 2: the heading now reads "Q2 results" and the activation rate is 44%. Nothing else changed.'
