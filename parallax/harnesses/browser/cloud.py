"""Browser Use Cloud lifecycle, CDP discovery, cleanup, and cost lines.

The optional SDK is imported only when a hosted browser is selected. Tests can
inject a small client object, which keeps all lifecycle rules offline-testable.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import math
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from parallax import interrupts
from parallax.harnesses.browser.site import (
    RETRYABLE_STATUS,
    TRANSIENT_TRANSPORT,
    NotStarted,
    Unavailable,
    refusal,
)

CLOUD_BROWSER_MINUTES = 240  # the longest Browser Use lets a browser live
PROXY_SETTLE_SECONDS = 45.0
STOP_ATTEMPTS = 4


@dataclass
class Browser:
    cdp_url: str
    live_url: str | None = None
    browser_id: str | None = None
    session_id: str | None = None
    cost: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    open_seconds: float | None = None
    close_seconds: float | None = None


def _failure(what: str, error: BaseException, *, starting: bool = False) -> Exception:
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        if status in RETRYABLE_STATUS:
            return Unavailable(f"cloud {what} returned HTTP {status}")
        if starting and refusal(status):
            return NotStarted(f"cloud {what} returned HTTP {status}")
        return RuntimeError(f"cloud {what} returned HTTP {status}")
    if isinstance(error, TRANSIENT_TRANSPORT):
        return Unavailable(f"cloud {what} failed ({type(error).__name__})")
    return RuntimeError(f"cloud {what} failed ({type(error).__name__})")


def cost_lines(view: object | None, *, settled: bool = True) -> tuple[dict[str, Any], ...]:
    def dollars(name: str) -> float | None:
        value = getattr(view, name, None) if view is not None else None
        return None if value is None else float(str(value))

    browser, proxy = dollars("browser_cost"), dollars("proxy_cost")
    return (
        {"component": "browser", "basis": "metered" if browser is not None else "unknown", "dollars": browser},
        {
            "component": "proxy",
            "basis": "metered" if proxy is not None and settled else "unsettled" if proxy is not None else "unknown",
            "dollars": proxy,
        },
    )


def with_stopped_browser_cost(
    lines: list[Mapping[str, Any]], stopped: tuple[dict[str, Any], ...]
) -> list[Mapping[str, Any]]:
    """The run's cost lines with the browser and proxy read after the browser stopped in place of the read taken when
    the run ended. While the browser is open Browser Use reports a placeholder browser charge and no proxy traffic,
    so a settled stopped figure always wins. An unknown or unsettled one (the post-stop read failed, so the proxy may
    still be counting) keeps the earlier read."""
    known = {line["component"]: line for line in stopped if line["basis"] == "metered"}
    present = {line.get("component") for line in lines}
    return [
        *(known.get(line.get("component"), line) for line in lines),
        *(line for line in stopped if line["component"] not in present),
    ]


async def devtools_websocket(http: httpx.AsyncClient, cdp_url: str) -> str:
    if cdp_url.startswith(("ws://", "wss://")):
        return cdp_url
    try:
        response = await http.get(f"{cdp_url.rstrip('/')}/json/version", timeout=30)
        response.raise_for_status()
        address = response.json().get("webSocketDebuggerUrl")
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in RETRYABLE_STATUS:
            raise Unavailable(f"cloud browser discovery returned HTTP {error.response.status_code}") from None
        raise _failure("browser discovery", error) from None
    if not isinstance(address, str) or not address:
        raise RuntimeError("cloud browser discovery did not return a DevTools address")
    return address


_open: dict[str, Any] = {}
"""Every cloud browser this process opened and has not yet seen stop, with the client that opened it (None: a worker
process opened it, so a client is made from the environment to stop it)."""


def opened(browser_id: str, client: Any | None = None) -> None:
    """Track a browser until it is seen stopped, so an interrupted run stops it first (`stop_open_browsers`)."""
    interrupts.on_interrupt(stop_open_browsers)
    _open[browser_id] = client


async def stop_open_browsers() -> None:
    """The run was interrupted: stop every open browser now, all at once and one attempt each. A trial's own stop
    runs only as its task unwinds, behind its worker's exit, and a cancelled CI job kills the process within seconds.
    A browser stopped here is forgotten, so its trial's own stop settles its cost instead of stopping it again."""
    browsers = dict(_open)
    fallback: Any | None = None
    if None in browsers.values():
        with contextlib.suppress(Exception):
            fallback = _client()

    async def stop(browser_id: str, client: Any | None) -> None:
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.browsers.stop(browser_id)
            _open.pop(browser_id, None)

    try:
        await asyncio.gather(*(stop(browser_id, client or fallback) for browser_id, client in browsers.items()))
    finally:
        if fallback is not None:
            with contextlib.suppress(Exception):
                await fallback.close()


def browser_minutes(seconds_left: float | None) -> int:
    """How long a cloud browser for a turn may live: the turn's remaining time in whole minutes, rounded up, as Browser
    Use takes it; the longest it allows when the turn has no limit. A browser whose stop is missed (the process was
    killed) then stops itself once its turn could no longer use it, instead of billing for hours."""
    if seconds_left is None:
        return CLOUD_BROWSER_MINUTES
    return max(1, min(CLOUD_BROWSER_MINUTES, math.ceil(seconds_left / 60)))


async def _stop(client: Any, browser_id: str) -> object | None:
    for attempt in range(STOP_ATTEMPTS):
        try:
            stopped = await client.browsers.stop(browser_id)
            _open.pop(browser_id, None)
            return stopped
        except Exception as error:
            status = getattr(error, "status_code", None)
            retryable = isinstance(error, TRANSIENT_TRANSPORT) or status in RETRYABLE_STATUS
            if not retryable or attempt + 1 == STOP_ATTEMPTS:
                return None
            delay = getattr(error, "retry_after", None)
            await asyncio.sleep(float(delay) if isinstance(delay, int | float) else 2**attempt)
    return None


async def _settled(client: Any, browser_id: str) -> object | None:
    """The browser's cost read once it has stopped and its proxy figure settled; None when the read fails or the
    browser is still running, as it is when every stop attempt failed, so a still-counting cost is never final."""
    await asyncio.sleep(PROXY_SETTLE_SECONDS)
    try:
        view = await client.browsers.get(browser_id)
    except Exception:
        return None
    if view.status.value != "stopped":
        return None
    _open.pop(browser_id, None)
    return view


@contextlib.asynccontextmanager
async def cloud_browser(
    http: httpx.AsyncClient,
    *,
    client: Any | None = None,
    client_factory: Callable[[], Any] | None = None,
    screen: tuple[int, int] | None = None,
    minutes: int = CLOUD_BROWSER_MINUTES,
    proxy_country: str | None = "us",
) -> AsyncGenerator[Browser]:
    """Create a hosted browser that stops itself after `minutes`, yield its CDP URL, and always stop and settle it. `proxy_country` is sent whatever it is, so None is no proxy, not the vendor default."""
    owned_client = client is None
    if client is None:
        client = _client() if client_factory is None else client_factory()
    width, height = screen or (None, None)
    began = time.monotonic()
    created: object | None = None
    browser: Browser | None = None
    creation = asyncio.create_task(
        client.browsers.create(
            timeout=minutes,
            proxy_country_code=proxy_country,
            browser_screen_width=width,
            browser_screen_height=height,
        )
    )
    try:
        try:
            created = await asyncio.shield(creation)
        except Exception as error:
            raise _failure("browser create", error, starting=True) from None
        browser_id = str(created.id)
        opened(browser_id, client)
        cdp = getattr(created, "cdp_url", None)
        if not isinstance(cdp, str) or not cdp:
            raise RuntimeError("cloud browser started without a CDP URL")
        try:
            websocket = await devtools_websocket(http, cdp)
        except Exception:
            await asyncio.shield(_stop(client, browser_id))
            raise
        browser = Browser(
            cdp_url=websocket,
            live_url=getattr(created, "live_url", None),
            browser_id=browser_id,
            open_seconds=round(time.monotonic() - began, 2),
        )
        try:
            yield browser
        finally:
            closing = time.monotonic()
            # An interrupt may have stopped it already; its cost settles all the same.
            stopped = await asyncio.shield(_stop(client, browser_id)) if browser_id in _open else None
            browser.close_seconds = round(time.monotonic() - closing, 2)
            settled = await asyncio.shield(_settled(client, browser_id)) if browser_id not in _open else None
            browser.cost = cost_lines(settled or stopped, settled=settled is not None)
    finally:
        await asyncio.gather(creation, return_exceptions=True)
        if created is None and not creation.cancelled() and creation.exception() is None:
            late_browser = creation.result()
            late_id = getattr(late_browser, "id", None)
            if late_id is not None:
                await asyncio.shield(_stop(client, str(late_id)))
        if owned_client:
            with contextlib.suppress(Exception):
                await client.close()


async def stopped_browser_cost(browser_id: str, *, client: Any | None = None) -> tuple[dict[str, Any], ...]:
    """The settled cost of a cloud browser another process opened, read once it has stopped. Stopping again makes sure
    it stops even if that process could not; Browser Use refuses a second stop, which leaves the settled read."""
    owned_client = client is None
    if client is None:
        client = _client()
    try:
        stopped = await asyncio.shield(_stop(client, browser_id))
        settled = await asyncio.shield(_settled(client, browser_id))
        return cost_lines(settled or stopped, settled=settled is not None)
    finally:
        if owned_client:
            with contextlib.suppress(Exception):
                await client.close()


def _client() -> Any:
    module = importlib.import_module("browser_use_sdk.v4")
    client_name = "AsyncBrowserUse"
    return getattr(module, client_name)(api_key=_api_key())


def _api_key() -> str:
    import os

    key = os.environ.get("BROWSER_USE_API_KEY")
    if not key:
        raise RuntimeError("set BROWSER_USE_API_KEY for a hosted browser")
    return key
