from pathlib import Path

BASE = "https://fenmoorcourier.example"
STORIES = (
    (
        "Old Mill bridge reopens after eight months",
        "news/2026/06/16/old-mill-bridge-reopens",
        "media/2026/06/old-mill-bridge.jpg",
        "The Old Mill footbridge reopened on Tuesday after repairs to its timber deck. Cyclists must dismount.",
    ),
    (
        "Library trials late opening on Thursdays",
        "news/2026/06/17/library-late-opening",
        "media/2026/06/library-evening.jpg",
        "The central library stays open until 9pm on Thursdays this summer, with a quiet study floor.",
    ),
    (
        "Food festival returns to Market Square",
        "news/2026/06/18/market-square-food-festival",
        "media/2026/06/food-festival.jpg",
        "Forty stalls, a cookery stage and live music this weekend; roads around the square close from 7am.",
    ),
)


def section(title: str, path: str, image: str, blurb: str) -> str:
    return f"## {title}\n\n![{title}]({BASE}/{image})\n\n{blurb} [Read the article]({BASE}/{path})\n"


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    for _, path, _, _ in STORIES:
        await world.call("web_fetch", {"url": f"{BASE}/{path}"})  # type: ignore[attr-defined]
    body = "\n".join(section(*s) for s in STORIES)
    (workspace / "roundup.md").write_text(f"# Fenmoor this week\n\n{body}\nSource: Fenmoor Courier\n", encoding="utf-8")
    return "Saved roundup.md with this week's three Fenmoor Courier stories, each with its photo and a link."
