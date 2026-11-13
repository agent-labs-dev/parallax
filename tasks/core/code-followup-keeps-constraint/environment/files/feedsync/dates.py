from datetime import date


class DateError(ValueError):
    pass


def parse_feed_date(text: str) -> date:
    """Parse a date from a partner feed row."""
    try:
        return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise DateError(f"unrecognised date: {text!r}") from exc
