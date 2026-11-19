from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from parallax.harnesses.browser.site import (
    NAVIGATE_ATTEMPTS,
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
    """A CDP socket for one tab. Each navigation answers with the next of `navigated` and its document has the next of
    `statuses`; the last of each repeats."""

    def __init__(self, *navigated: dict[str, Any], statuses: tuple[int | None, ...] = (200,)) -> None:
        self.navigated, self.statuses = list(navigated), list(statuses)
        self.sent: list[dict[str, Any]] = []
        self._replies: list[str] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        method = message["method"]
        if method == "Page.navigate":
            result: dict[str, Any] = self.navigated.pop(0) if len(self.navigated) > 1 else self.navigated[0]
        elif method == "Runtime.evaluate":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            result = {"result": {"type": "number", "value": status}}
        else:
            result = {
                "Target.getTargets": {"targetInfos": [{"targetId": "tab", "type": "page", "url": "about:blank"}]},
                "Target.attachToTarget": {"sessionId": "s"},
            }[method]
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
    assert await browser_site_down("ws://browser", "https://site.test/start", backoff=0) is None
    assert [m["method"] for m in browser.sent if m["method"] == "Page.navigate"] == ["Page.navigate"]
    assert browser.closed


async def test_a_navigation_that_fails_every_attempt_is_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = _Browser(
        {"frameId": "f", "errorText": "net::ERR_CONNECTION_REFUSED"},
        {"frameId": "f", "errorText": "net::ERR_NAME_NOT_RESOLVED"},
    )
    _serving(monkeypatch, browser)
    # The reason is the last attempt's.
    assert (
        await browser_site_down("ws://browser", "https://site.test/start", backoff=0)
        == "site unavailable: https://site.test/start unreachable in the trial's browser (net::ERR_NAME_NOT_RESOLVED)"
    )
    assert sum(m["method"] == "Page.navigate" for m in browser.sent) == NAVIGATE_ATTEMPTS


async def test_a_navigation_that_fails_once_and_then_commits_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh cloud browser's first connection can fail on its own, and the next one load the page.
    browser = _Browser(
        {"frameId": "f", "errorText": "net::ERR_HTTP2_PROTOCOL_ERROR"}, {"frameId": "f", "loaderId": "l"}
    )
    _serving(monkeypatch, browser)
    assert await browser_site_down("ws://browser", "https://site.test/start", backoff=0) is None
    assert sum(m["method"] == "Page.navigate" for m in browser.sent) == 2


@pytest.mark.parametrize(("status", "down"), [(503, True), (403, False), (200, False)])
async def test_a_retryable_document_status_is_an_outage(
    monkeypatch: pytest.MonkeyPatch, status: int, down: bool
) -> None:
    _serving(monkeypatch, _Browser({"frameId": "f", "loaderId": "l"}, statuses=(status,)))
    reason = await browser_site_down("ws://browser", "https://site.test/start", backoff=0)
    assert reason == (
        f"site unavailable: https://site.test/start answered HTTP {status} in the trial's browser" if down else None
    )
