from __future__ import annotations

import asyncio

import httpx
import pytest
from parallax.harnesses.browser.site import (
    RETRYABLE_STATUS,
    SiteWatch,
    Unavailable,
    refusal,
    retryable_status,
    site_down,
)


async def test_site_down_classifies_5xx_with_mock_transport() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        assert (
            await site_down(client, "https://site.test/start")
            == "site unavailable: https://site.test/start answered HTTP 503"
        )


async def test_site_down_classifies_transport_failures() -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        reason = await site_down(client, "https://site.test/start")
    assert reason is not None and "ConnectError" in reason


async def test_site_down_classifies_access_blocks_before_agent_actions() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(403))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await site_down(client, "https://site.test/start") == (
            "site unavailable: https://site.test/start answered HTTP 403"
        )


async def test_site_watch_records_an_overlapping_stall() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        watch = SiteWatch(client, ["https://site.test/start"], probe_seconds=0.01, stall_seconds=0)
        running = asyncio.create_task(watch.run())
        await asyncio.sleep(0.02)
        await watch.stop()
        await asyncio.wait_for(running, 1)
        assert await watch.stalled("https://site.test/start", 0, 10**12) is not None


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
def test_retryable_statuses_are_explicit_and_refusals_are_not(status: int) -> None:
    assert retryable_status(status)
    assert not refusal(status)
    assert refusal(401)


def test_provider_outage_exception_has_a_stable_type() -> None:
    assert issubclass(Unavailable, RuntimeError)
