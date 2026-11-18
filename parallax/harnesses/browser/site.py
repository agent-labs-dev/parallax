"""Site health and retry classification for browser providers."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

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


NAVIGATE_SECONDS = 60.0
"""How long the trial's browser has to commit the start page before the site counts as not answering. Chrome gives a
navigation no deadline of its own; this is far past a slow site's first byte, and only bounds the check."""

NAVIGATE_ATTEMPTS = 3
"""How many times the start page is loaded before the site counts as down. One failed navigation is not an outage: a
fresh cloud browser's first connection can fail on its own (a reset HTTP/2 stream, a proxy hop that drops) and the
next one load the page, and a browser harness's own navigation retries such a failure rather than giving up. Each
retry waits twice as long as the one before it, from `NAVIGATE_BACKOFF_SECONDS`, so a site that fails every attempt
has failed over several seconds and several connections."""

NAVIGATE_BACKOFF_SECONDS = 1.0

_DOCUMENT_STATUS = "performance.getEntriesByType('navigation')[0]?.responseStatus ?? null"


async def browser_site_down(
    cdp_url: str,
    url: str,
    *,
    seconds: float = NAVIGATE_SECONDS,
    attempts: int = NAVIGATE_ATTEMPTS,
    backoff: float = NAVIGATE_BACKOFF_SECONDS,
) -> str | None:
    """Open `url` in the trial's own browser and return an outage reason, or ``None`` when the site answers.

    The browser judges the site, from the network and with the client the trial browses with: a site behind bot
    protection refuses a plain HTTP request from the host and loads here. A navigation error is the browser's own
    (`net::ERR_NAME_NOT_RESOLVED`, `net::ERR_CONNECTION_REFUSED`, ...), and a committed document with a retryable
    status is an outage as `retryable_status` defines it. The site is down only when every one of `attempts` loads
    fails, and the reason is the last attempt's. The page is left on `url`, where the trial starts."""
    from parallax.harnesses.browser.observer import CDPConnection, _connect, page_targets

    websocket = await _connect(cdp_url)
    cdp = CDPConnection(websocket)
    try:
        targets = page_targets((await cdp.command("Target.getTargets")).get("targetInfos", []))
        if targets:
            target_id = targets[0]["targetId"]
        else:
            target_id = (await cdp.command("Target.createTarget", {"url": "about:blank"}))["targetId"]
        attached = await cdp.command("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session = attached["sessionId"]
        reason: str | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(backoff * 2 ** (attempt - 1))
            reason = await _load(cdp, session, url, seconds)
            if reason is None:
                return None
        return reason
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


async def _load(cdp: Any, session: str, url: str, seconds: float) -> str | None:
    """Load `url` once in the attached tab: why it failed, or ``None`` when it committed a document that answers."""
    try:
        async with asyncio.timeout(seconds):
            navigated = await cdp.command("Page.navigate", {"url": url}, session=session)
            error = navigated.get("errorText")
            if isinstance(error, str) and error:
                return f"site unavailable: {url} unreachable in the trial's browser ({error})"
            evaluated = await cdp.command(
                "Runtime.evaluate", {"expression": _DOCUMENT_STATUS, "returnByValue": True}, session=session
            )
    except TimeoutError:
        return f"site unavailable: {url} did not answer in the trial's browser within {seconds:g} s"
    status = evaluated.get("result", {}).get("value")
    if isinstance(status, int) and retryable_status(status):
        return f"site unavailable: {url} answered HTTP {status} in the trial's browser"
    return None
