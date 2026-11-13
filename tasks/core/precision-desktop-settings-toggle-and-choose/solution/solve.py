import json
from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    (workspace / "preferences.json").write_text(
        json.dumps({"notifications": True, "theme": "Dark", "launch_on_login": True}) + "\n"
    )
    return "Saved: notifications on, Dark theme. Launch on login is unchanged."
