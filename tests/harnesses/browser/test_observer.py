from __future__ import annotations

import json
from typing import Any

from parallax.harnesses.browser import observer


class ReplayWebSocket:
    def __init__(self, targets: list[dict[str, Any]] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.targets = targets or [{"targetId": "tab-1", "type": "page", "url": "https://site.test/"}]

    async def send(self, value: str) -> None:
        self.sent.append(json.loads(value))

    async def recv(self) -> str:
        request = self.sent[-1]
        result: dict[str, Any]
        match request["method"]:
            case "Target.getTargets":
                result = {"targetInfos": self.targets}
            case "Target.attachToTarget":
                result = {"sessionId": "session-1"}
            case "Runtime.evaluate" if "location.href" in request["params"]["expression"]:
                result = {"result": {"value": {"url": "https://site.test/", "title": "Example", "text": "Hello"}}}
            case "Runtime.evaluate":
                result = {"result": {"value": [{"label": "Search", "value": ""}]}}
            case "Accessibility.getFullAXTree":
                result = {"nodes": [{"role": {"value": "button"}, "name": {"value": "Submit"}}]}
            case "Page.captureScreenshot":
                result = {"data": "aGVsbG8="}
            case _:
                result = {}
        return json.dumps({"id": request["id"], "result": result})

    async def close(self) -> None:
        return None


async def test_plain_observer_reads_page_dom_accessibility_and_png(monkeypatch: Any) -> None:
    websocket = ReplayWebSocket()

    async def connect(_: str) -> ReplayWebSocket:
        return websocket

    monkeypatch.setattr(observer, "_connect", connect)
    evidence = await observer.PlainCDPObserver().observe("ws://browser.test/devtools", url="https://site.test/")
    assert evidence["url"] == "https://site.test/"
    assert {item["label"] for item in evidence["controls"]} == {"Search", "Submit"}
    assert evidence["screenshot_png_b64"] == "aGVsbG8="


def test_target_selection_fails_closed_for_ambiguous_pages() -> None:
    targets = [{"type": "page", "targetId": "a"}, {"type": "page", "targetId": "b"}]
    try:
        observer.select_target(targets)
    except RuntimeError as error:
        assert "expected one final page" in str(error)
    else:
        raise AssertionError("ambiguous page selection should fail closed")


async def test_plain_observer_reads_every_tab_when_none_is_the_final_page(monkeypatch: Any) -> None:
    targets = [
        {"targetId": "a", "type": "page", "url": "https://site.test/a"},
        {"targetId": "b", "type": "page", "url": "https://site.test/b"},
        {"targetId": "w", "type": "service_worker", "url": "https://site.test/sw.js"},
    ]
    websocket = ReplayWebSocket(targets)

    async def connect(_: str) -> ReplayWebSocket:
        return websocket

    monkeypatch.setattr(observer, "_connect", connect)
    try:
        await observer.PlainCDPObserver().observe("ws://browser.test/devtools")
    except observer.AmbiguousFinalPage as error:
        assert len(error.tabs) == 2
        assert error.tabs[0]["title"] == "Example"
    else:
        raise AssertionError("two open tabs name no final page")
    attached = [m["params"]["targetId"] for m in websocket.sent if m["method"] == "Target.attachToTarget"]
    assert attached == ["a", "b"]
