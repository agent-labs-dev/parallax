from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from parallax.harnesses.browser.site import (
    RETRYABLE_STATUS,
    Unavailable,
    browser_site_down,
    refusal,
    retryable_status,
)


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
def test_retryable_statuses_are_explicit_and_refusals_are_not(status: int) -> None:
    assert retryable_status(status)
    assert not refusal(status)
    assert refusal(401)


def test_provider_outage_exception_has_a_stable_type() -> None:
    assert issubclass(Unavailable, RuntimeError)


class _Browser:
    """A CDP socket for one tab whose navigation answers with `navigated` and whose document has `status`."""

    def __init__(self, navigated: dict[str, Any], status: int | None = 200) -> None:
        self.navigated, self.status = navigated, status
        self.sent: list[dict[str, Any]] = []
        self._replies: list[str] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        result: dict[str, Any] = {
            "Target.getTargets": {"targetInfos": [{"targetId": "tab", "type": "page", "url": "about:blank"}]},
            "Target.attachToTarget": {"sessionId": "s"},
            "Page.navigate": self.navigated,
            "Runtime.evaluate": {"result": {"type": "number", "value": self.status}},
        }[message["method"]]
        self._replies.append(json.dumps({"id": message["id"], "result": result}))

    async def recv(self) -> str:
        return self._replies.pop(0)

    async def close(self) -> None:
        self.closed = True


def _serving(monkeypatch: pytest.MonkeyPatch, browser: _Browser) -> None:
    async def connect(_: str) -> _Browser:
        return browser

    monkeypatch.setattr("parallax.harnesses.browser.observer._connect", connect)


async def test_a_site_that_refuses_plain_http_but_loads_in_the_browser_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", refused)
    browser = _Browser({"frameId": "f", "loaderId": "l"})
    _serving(monkeypatch, browser)
    assert await browser_site_down("ws://browser", "https://site.test/start") is None
    assert [m["method"] for m in browser.sent if m["method"] == "Page.navigate"] == ["Page.navigate"]
    assert browser.closed


async def test_a_navigation_error_in_the_browser_is_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    _serving(monkeypatch, _Browser({"frameId": "f", "errorText": "net::ERR_NAME_NOT_RESOLVED"}))
    assert (
        await browser_site_down("ws://browser", "https://site.test/start")
        == "site unavailable: https://site.test/start unreachable in the trial's browser (net::ERR_NAME_NOT_RESOLVED)"
    )


@pytest.mark.parametrize(("status", "down"), [(503, True), (403, False), (200, False)])
async def test_a_retryable_document_status_is_an_outage(
    monkeypatch: pytest.MonkeyPatch, status: int, down: bool
) -> None:
    _serving(monkeypatch, _Browser({"frameId": "f", "loaderId": "l"}, status))
    reason = await browser_site_down("ws://browser", "https://site.test/start")
    assert reason == (
        f"site unavailable: https://site.test/start answered HTTP {status} in the trial's browser" if down else None
    )
