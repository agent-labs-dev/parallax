from feedsync.dates import DateError, parse_feed_date


def accept(rows: list[dict[str, str]]) -> tuple[list[dict[str, object]], list[str]]:
    """Split rows into parsed ones and the raw dates that were rejected."""
    good: list[dict[str, object]] = []
    rejected: list[str] = []
    for row in rows:
        try:
            good.append({**row, "date": parse_feed_date(row["date"])})
        except DateError:
            rejected.append(row["date"])
    return good, rejected
