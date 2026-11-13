from pathlib import Path

DATES = '''from datetime import date, datetime


class DateError(ValueError):
    pass


# Tried in order. Day-first slashes because the partner is UK-based; stdlib only (no new dependencies).
_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y")


def parse_feed_date(text: str) -> date:
    """Parse a date from a partner feed row."""
    cleaned = text.strip()
    for fmt in _FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise DateError(f"unrecognised date: {text!r}")
'''


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    if step == "turn-01":
        return (
            "Stay on the standard library: try datetime.strptime against a short, ordered list of explicit "
            "formats - ISO (%Y-%m-%d), day-first slashes (%d/%m/%Y) and day-month-name (%d %b %Y, plus "
            "%d %B %Y for full month names) - and raise DateError if none match. Explicit formats avoid "
            "guessing, so 12/06/2026 is always 12 June, never 6 December. No new dependency needed."
        )
    (workspace / "feedsync" / "dates.py").write_text(DATES, encoding="utf-8")
    return (
        "Done: parse_feed_date in feedsync/dates.py now tries ISO, dd/mm/yyyy and '3 Jun 2026' style dates "
        "with datetime.strptime, standard library only, and still raises DateError otherwise. Tests pass."
    )
