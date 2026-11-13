"""Plain DevTools Protocol final-page observation.

No harness SDK or product page reader is involved. The observer selects one page,
reads its DOM and accessibility tree, captures the viewport, and returns plain
JSON suitable for the browser evidence model.
"""

from __future__ import annotations

import base64
import contextlib
import importlib
import itertools
import json
from collections.abc import Mapping, Sequence
from importlib.metadata import entry_points
from typing import Any, Protocol

import httpx

OBSERVER_ENTRY_POINT_GROUP = "parallax.browser_observers"


class Observer(Protocol):
    name: str

    async def observe(self, cdp_url: str, *, url: str | None = None) -> dict[str, Any]: ...


class CDPConnection:
    """Small JSON-over-WebSocket CDP client with no browser-specific dependency."""

    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self._ids = itertools.count(1)

    async def command(self, method: str, params: Mapping[str, Any] | None = None, *, session: str | None = None) -> Any:
        command_id = next(self._ids)
        message: dict[str, Any] = {"id": command_id, "method": method}
        if params:
            message["params"] = dict(params)
        if session is not None:
            message["sessionId"] = session
        await self.websocket.send(json.dumps(message))
        while True:
            raw = await self.websocket.recv()
            item = json.loads(raw)
            if item.get("id") != command_id:
                continue
            if "error" in item:
                raise RuntimeError(f"CDP {method} failed")
            return item.get("result", {})


async def _connect(cdp_url: str) -> Any:
    if cdp_url.startswith(("http://", "https://")):
        async with httpx.AsyncClient() as http:
            response = await http.get(f"{cdp_url.rstrip('/')}/json/version", timeout=30)
            response.raise_for_status()
            cdp_url = str(response.json()["webSocketDebuggerUrl"])
    try:
        module = importlib.import_module("websockets.asyncio.client")
        connect_name = "connect"
        connect = getattr(module, connect_name)
    except ImportError:
        try:
            module = importlib.import_module("websockets.client")
            connect_name = "connect"
            connect = getattr(module, connect_name)
        except ImportError as error:
            raise RuntimeError("the browser extra is required for plain CDP observation") from error
    return await connect(cdp_url, max_size=64 * 1024 * 1024)


def select_target(targets: Sequence[Mapping[str, Any]], wanted_url: str | None = None) -> Mapping[str, Any]:
    pages = [target for target in targets if target.get("type") == "page"]
    if wanted_url is not None:
        named = [target for target in pages if target.get("url") == wanted_url]
        if len(named) == 1:
            return named[0]
    if len(pages) == 1:
        return pages[0]
    raise RuntimeError(f"expected one final page, found {len(pages)}")


def controls_from_dom(raw: object) -> list[dict[str, str | None]]:
    if not isinstance(raw, list):
        return []
    controls: list[dict[str, str | None]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        label, value = item.get("label"), item.get("value")
        if isinstance(label, str) and (isinstance(value, str) or value is None):
            controls.append({"label": label, "value": value})
    return controls


def controls_from_ax(raw: object) -> list[dict[str, str | None]]:
    if not isinstance(raw, list):
        return []
    interactive = {"button", "link", "textbox", "checkbox", "combobox", "listbox", "menuitem", "radio", "slider"}
    controls: list[dict[str, str | None]] = []
    for node in raw:
        if not isinstance(node, Mapping):
            continue
        role = node.get("role", {}).get("value") if isinstance(node.get("role"), Mapping) else None
        name = node.get("name", {}).get("value") if isinstance(node.get("name"), Mapping) else None
        value = node.get("value", {}).get("value") if isinstance(node.get("value"), Mapping) else None
        if isinstance(role, str) and role in interactive and isinstance(name, str) and name:
            controls.append({"label": name, "value": value if isinstance(value, str) else None})
    return controls


DOM_SCRIPT = """
(() => {
  const visible = e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = e => e.getAttribute('aria-label') || e.labels?.[0]?.innerText?.trim() ||
    e.innerText?.trim() || e.getAttribute('title') || e.getAttribute('name') || '';
  return [...document.querySelectorAll('button,input,textarea,select,[role="button"],[role="link"],[contenteditable="true"]')]
    .filter(visible).map(e => ({label: label(e), value: e.value ?? null})).filter(e => e.label);
})()
"""
PAGE_SCRIPT = """
(() => ({url: location.href, title: document.title || '', text: (document.body?.innerText || '').trim()}))()
"""


class PlainCDPObserver:
    name = "plain-cdp"

    async def observe(self, cdp_url: str, *, url: str | None = None) -> dict[str, Any]:
        websocket = await _connect(cdp_url)
        cdp = CDPConnection(websocket)
        try:
            targets = (await cdp.command("Target.getTargets")).get("targetInfos", [])
            target = select_target(targets, url)
            attached = await cdp.command("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
            session = attached["sessionId"]
            page = await cdp.command(
                "Runtime.evaluate", {"expression": PAGE_SCRIPT, "returnByValue": True}, session=session
            )
            values = page.get("result", {}).get("value", {})
            if not isinstance(values, Mapping):
                raise RuntimeError("final page script returned no page")
            dom = await cdp.command(
                "Runtime.evaluate", {"expression": DOM_SCRIPT, "returnByValue": True}, session=session
            )
            ax = await cdp.command("Accessibility.getFullAXTree", session=session)
            shot = await cdp.command(
                "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}, session=session
            )
            controls = controls_from_dom(dom.get("result", {}).get("value"))
            for item in controls_from_ax(ax.get("nodes")):
                if item not in controls:
                    controls.append(item)
            image = shot.get("data")
            return {
                "url": values.get("url"),
                "title": values.get("title"),
                "text": values.get("text"),
                "screenshot_png_b64": image if isinstance(image, str) else None,
                "controls": controls,
            }
        finally:
            with contextlib.suppress(Exception):
                await websocket.close()


def load_observer(name: str = "plain-cdp") -> Observer:
    matches = [ep for ep in entry_points(group=OBSERVER_ENTRY_POINT_GROUP) if ep.name == name]
    if len(matches) > 1:
        raise RuntimeError(f"multiple browser observers registered for {name!r}")
    if matches:
        observer = matches[0].load()()
    else:
        if name != PlainCDPObserver.name:
            raise RuntimeError(f"unknown browser observer {name!r}")
        observer = PlainCDPObserver()
    if not callable(getattr(observer, "observe", None)):
        raise TypeError(f"browser observer {name!r} has no observe method")
    return observer


def screenshot_bytes(evidence: Mapping[str, Any]) -> bytes | None:
    value = evidence.get("screenshot_png_b64")
    if not isinstance(value, str):
        return None
    with contextlib.suppress(ValueError):
        return base64.b64decode(value)
    return None
