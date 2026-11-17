from __future__ import annotations

import httpx
import pytest
from parallax.harnesses.browser.site import (
    RETRYABLE_STATUS,
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


@pytest.mark.parametrize("status", [401, 403])
async def test_site_down_does_not_block_browser_on_http_access_refusal(status: int) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(status))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await site_down(client, "https://site.test/start") is None


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
def test_retryable_statuses_are_explicit_and_refusals_are_not(status: int) -> None:
    assert retryable_status(status)
    assert not refusal(status)
    assert refusal(401)


def test_provider_outage_exception_has_a_stable_type() -> None:
    assert issubclass(Unavailable, RuntimeError)
