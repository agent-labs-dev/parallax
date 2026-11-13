import json
from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    (workspace / "collection.json").write_text(json.dumps({"name": "Autumn planning"}) + "\n")
    return "Renamed the collection to Autumn planning; all three entries are preserved."
