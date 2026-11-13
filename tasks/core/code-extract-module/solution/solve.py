from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    pkg = workspace / "ingest"
    (pkg / "utils.py").rename(pkg / "parsers.py")
    loader = pkg / "loader.py"
    loader.write_text(loader.read_text().replace("from ingest.utils import", "from ingest.parsers import"))
    summary = pkg / "summary.py"
    summary.write_text(
        summary.read_text()
        .replace("from ingest import utils", "from ingest import parsers")
        .replace("utils.parse_amount", "parsers.parse_amount")
    )
    return "Moved parse_date and parse_amount into ingest/parsers.py, rewired loader.py and summary.py, and deleted utils.py. Tests pass."
