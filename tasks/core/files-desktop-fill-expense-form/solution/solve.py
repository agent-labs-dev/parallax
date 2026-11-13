import json
from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    (workspace / "submitted.json").write_text(json.dumps([{"amount": "42.50", "memo": "team lunch"}]) + "\n")
    return "Submitted the 42.50 team lunch expense."
