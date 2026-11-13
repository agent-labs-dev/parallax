from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    app = workspace / "app.py"
    app.write_text(app.read_text().replace("sum(items[1:])", "sum(items)"))
    return "Fixed total() to sum every item."
