"""Site health and retry classification for browser providers."""

from __future__ import annotations

import httpx

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529})
TRANSIENT_TRANSPORT: tuple[type[httpx.HTTPError], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
    httpx.DecodingError,
)


class NotStarted(RuntimeError):
    """A provider refused a start request without creating a run."""


class Unavailable(RuntimeError):
    """A provider or site failure can plausibly clear on a later attempt."""


def retryable_status(status: int | None) -> bool:
    return status in RETRYABLE_STATUS


def refusal(status: int | None) -> bool:
    return status is not None and status >= 400 and status not in RETRYABLE_STATUS
