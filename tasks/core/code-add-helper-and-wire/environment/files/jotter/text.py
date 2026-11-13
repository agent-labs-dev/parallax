"""Small text helpers used by the CLI."""


def truncate(text: str, width: int) -> str:
    """Shorten text to width characters, marking the cut with an ellipsis."""
    if len(text) <= width:
        return text
    return text[: max(width - 1, 0)] + "…"
