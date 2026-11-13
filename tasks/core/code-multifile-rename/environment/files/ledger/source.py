"""Reads ledger rows out of the CSV exports the finance tool drops in exports/."""

import csv
from pathlib import Path


def fetch_rows(folder: str | Path, table: str, *, limit: int | None = None) -> list[dict[str, str]]:
    """Rows of exports/<table>.csv as dicts, in file order."""
    with open(Path(folder) / f"{table}.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return rows if limit is None else rows[:limit]
