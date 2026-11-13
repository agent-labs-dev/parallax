from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    path = workspace / "relay" / "settings.py"
    path.write_text(path.read_text().replace("DEFAULT_PORT = 8000", "DEFAULT_PORT = 8080"))
    return "Changed DEFAULT_PORT in relay/settings.py from 8000 to 8080. It takes effect on the next restart."
