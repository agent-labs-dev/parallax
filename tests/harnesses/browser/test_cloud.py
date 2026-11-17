from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from parallax.harnesses.browser import cloud
from parallax.harnesses.browser.cloud import cost_lines, devtools_websocket, with_stopped_browser_cost
from parallax.harnesses.browser.site import Unavailable


async def test_cdp_discovery_uses_mock_transport() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"webSocketDebuggerUrl": "ws://browser.test/devtools"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        assert await devtools_websocket(client, "https://browser.test") == "ws://browser.test/devtools"


async def test_cdp_discovery_turns_retryable_http_into_unavailable() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(Unavailable):
            await devtools_websocket(client, "https://browser.test")


def test_cloud_cost_lines_keep_unknown_and_unsettled_distinct() -> None:
    view = SimpleNamespace(browser_cost="0.12", proxy_cost=None)
    assert cost_lines(view, settled=False) == (
        {"component": "browser", "basis": "metered", "dollars": 0.12},
        {"component": "proxy", "basis": "unknown", "dollars": None},
    )


async def test_cloud_browser_stops_and_settles_the_owned_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    class Browsers:
        def __init__(self) -> None:
            self.stopped: list[str] = []

        async def create(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(id="browser-1", cdp_url="ws://browser.test/devtools", live_url="https://live.test")

        async def stop(self, browser_id: str) -> SimpleNamespace:
            self.stopped.append(browser_id)
            return SimpleNamespace(browser_cost="0.10", proxy_cost="0.02")

        async def get(self, browser_id: str) -> SimpleNamespace:
            assert browser_id == "browser-1"
            return SimpleNamespace(browser_cost="0.10", proxy_cost="0.02")

    class Client:
        def __init__(self) -> None:
            self.browsers = Browsers()

    client = Client()
    monkeypatch.setattr(cloud, "PROXY_SETTLE_SECONDS", 0)
    async with httpx.AsyncClient() as http:
        async with cloud.cloud_browser(http, client=client) as browser:
            assert browser.cdp_url == "ws://browser.test/devtools"
        assert client.browsers.stopped == ["browser-1"]
        assert browser.cost[0]["dollars"] == 0.10


def test_the_cost_read_after_the_browser_stopped_replaces_the_open_browser_placeholder() -> None:
    # Measured on a cloud run (parallax#89): $0.08 browser and no proxy while open, settled after the stop.
    at_answer = [
        {"component": "model", "dollars": "0.001866"},
        {"component": "browser", "dollars": "0.080000"},
        {"component": "proxy", "dollars": "0.000000"},
    ]
    stopped = cost_lines(SimpleNamespace(browser_cost="0.000333", proxy_cost="0.000139"))
    assert with_stopped_browser_cost(at_answer, stopped) == [
        {"component": "model", "dollars": "0.001866"},
        *stopped,
    ]

    # When the read after the stop fails, the stop response's proxy figure may still be counting: keep the earlier one.
    unsettled = cost_lines(SimpleNamespace(browser_cost="0.000333", proxy_cost="0.000050"), settled=False)
    assert with_stopped_browser_cost(at_answer, unsettled) == [
        {"component": "model", "dollars": "0.001866"},
        unsettled[0],
        {"component": "proxy", "dollars": "0.000000"},
    ]
    # A browser that could not be read after stopping keeps the earlier figures.
    assert with_stopped_browser_cost(at_answer, cost_lines(None)) == at_answer
