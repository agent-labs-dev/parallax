"""The sidecar's egress proxy tunnels only HTTPS to the model endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from parallax import egress
from parallax.models import Isolation
from parallax.runner import Services
from parallax.task import load_task

TASKS = Path(__file__).resolve().parents[2] / "tasks" / "core"


@pytest.mark.parametrize(
    ("host", "port", "ok"),
    [
        ("openrouter.ai", 443, True),
        ("api.OpenRouter.ai.", 443, True),
        ("openrouter.ai", 80, False),
        ("evilopenrouter.ai", 443, False),
        ("openrouter.ai.evil.example", 443, False),
        ("example.com", 443, False),
    ],
)
def test_only_the_model_endpoints_on_443_are_allowed(host: str, port: int, ok: bool) -> None:
    assert egress.allowed(host, port) is ok


async def test_a_refused_target_gets_403_and_a_plain_request_is_not_proxied() -> None:
    server = await asyncio.start_server(egress.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        for request in (b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", b"GET http://openrouter.ai/ HTTP/1.1\r\n\r\n"):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(request)
            assert (await reader.readline()).startswith(b"HTTP/1.1 403")
            writer.close()
    finally:
        server.close()


def test_a_harness_launched_desktop_box_gets_the_proxy_and_no_other_task_does() -> None:
    svc = Services(isolation=Isolation.DOCKER, public_url=None, need_meter=False, run_id="t")
    svc.egress_internal = "http://sidecar:7803"
    plain = load_task(TASKS / "files-desktop-fill-expense-form")
    assert svc.egress_env(plain, Isolation.DOCKER) == {}
    assert plain.meta.desktop is not None
    desktop = plain.meta.desktop.model_copy(update={"session": "hyprland", "launch_by": "harness"})
    harness = plain.model_copy(update={"meta": plain.meta.model_copy(update={"desktop": desktop})})
    env = svc.egress_env(harness, Isolation.DOCKER)
    assert env["HTTPS_PROXY"] == "http://sidecar:7803"
    assert "sidecar" in env["NO_PROXY"].split(",")  # the meter and the world stay direct
    assert svc.egress_env(harness, Isolation.HOST) == {}
