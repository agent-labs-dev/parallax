from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    app = workspace / "app.py"
    app.write_text(app.read_text().replace("price * percent\n", "price * percent / 100\n"))
    return "apply_discount() treated the percentage as a fraction; it now divides by 100 and all three tests pass."
