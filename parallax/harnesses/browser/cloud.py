"""Browser Use Cloud lifecycle, CDP discovery, cleanup, and cost lines.

The optional SDK is imported only when a hosted browser is selected. Tests can
inject a small client object, which keeps all lifecycle rules offline-testable.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from parallax.harnesses.browser.site import (
    RETRYABLE_STATUS,
    TRANSIENT_TRANSPORT,
    NotStarted,
    Unavailable,
    refusal,
)

CLOUD_BROWSER_MINUTES = 240
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


async def _stop(client: Any, browser_id: str) -> object | None:
    for attempt in range(STOP_ATTEMPTS):
        try:
            return await client.browsers.stop(browser_id)
        except Exception as error:
            status = getattr(error, "status_code", None)
            retryable = isinstance(error, TRANSIENT_TRANSPORT) or status in RETRYABLE_STATUS
            if not retryable or attempt + 1 == STOP_ATTEMPTS:
                return None
            delay = getattr(error, "retry_after", None)
            await asyncio.sleep(float(delay) if isinstance(delay, int | float) else 2**attempt)
    return None


async def _settled(client: Any, browser_id: str) -> object | None:
    await asyncio.sleep(PROXY_SETTLE_SECONDS)
    with contextlib.suppress(Exception):
        return await client.browsers.get(browser_id)
    return None


@contextlib.asynccontextmanager
async def cloud_browser(
    http: httpx.AsyncClient,
    *,
    client: Any | None = None,
    client_factory: Callable[[], Any] | None = None,
    screen: tuple[int, int] | None = None,
) -> AsyncGenerator[Browser]:
    """Create a hosted browser, yield its CDP URL, and always stop and settle it."""
    owned_client = client is None
    if client is None:
        if client_factory is None:
            module = importlib.import_module("browser_use_sdk.v4")
            client_name = "AsyncBrowserUse"
            client_type = getattr(module, client_name)
            client = client_type(api_key=_api_key())
        else:
            client = client_factory()
    width, height = screen or (None, None)
    began = time.monotonic()
    created: object | None = None
    browser: Browser | None = None
    creation = asyncio.create_task(
        client.browsers.create(
            timeout=CLOUD_BROWSER_MINUTES,
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
            stopped = await asyncio.shield(_stop(client, browser_id))
            browser.close_seconds = round(time.monotonic() - closing, 2)
            settled = await asyncio.shield(_settled(client, browser_id)) if stopped is not None else None
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


async def stop_cloud_browser(browser_id: str, *, client: Any | None = None) -> None:
    owned_client = client is None
    if client is None:
        module = importlib.import_module("browser_use_sdk.v4")
        client_name = "AsyncBrowserUse"
        client_type = getattr(module, client_name)
        client = client_type(api_key=_api_key())
    try:
        await _stop(client, browser_id)
    finally:
        if owned_client:
            with contextlib.suppress(Exception):
                await client.close()


async def metered_cost(browser_id: str, *, client: Any | None = None) -> tuple[dict[str, Any], ...]:
    owned_client = client is None
    if client is None:
        module = importlib.import_module("browser_use_sdk.v4")
        client_name = "AsyncBrowserUse"
        client_type = getattr(module, client_name)
        client = client_type(api_key=_api_key())
    try:
        return cost_lines(await _settled(client, browser_id))
    finally:
        if owned_client:
            with contextlib.suppress(Exception):
                await client.close()


def _api_key() -> str:
    import os

    key = os.environ.get("BROWSER_USE_API_KEY")
    if not key:
        raise RuntimeError("set BROWSER_USE_API_KEY for a hosted browser")
    return key
