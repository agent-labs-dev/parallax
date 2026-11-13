"""Cross-slice contract: task loader -> world server -> real services -> MCP client -> evidence -> checks.

Everything the runner will wire together, minus the harness: a task registered through
the admin API, tools called over MCP in-process, evidence fetched over the control API
(so it survives a JSON round trip), and the task's own checks graded against it.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import httpx2
import yaml
from parallax.models import JsonObject, WorldEvidence, WorldSeed
from parallax.world.client import ControlClient, WorldClient, admin_register
from parallax.world.core import World
from parallax.world.server import WorldServer
from pydantic import TypeAdapter
from starlette.applications import Starlette

BASE = "http://sidecar:7801"
ADMIN = "admin-token"
MINI_MAIL = Path(__file__).parents[1] / "core" / "fixtures" / "tasks" / "mini-mail"
ORACLE_CALLS: TypeAdapter[list[JsonObject]] = TypeAdapter(list[JsonObject])


@contextlib.asynccontextmanager
async def running(server: WorldServer) -> AsyncIterator[Starlette]:
    application = server.app()
    async with application.router.lifespan_context(application):
        yield application


async def run_mini_mail(extra_calls: tuple[tuple[str, JsonObject], ...]) -> WorldEvidence:
    """Replay mini-mail's oracle (plus `extra_calls`) through the real stack; check id -> passed."""
    oracle = yaml.safe_load((MINI_MAIL / "solution" / "oracle.yaml").read_text())["steps"]["turn-01"]
    server = WorldServer(admin_token=ADMIN)
    async with running(server) as app:
        reg = await admin_register(BASE, ADMIN, str(MINI_MAIL), transport=httpx.ASGITransport(app=app))
        client = WorldClient(f"{BASE}/mcp", reg.token, transport=httpx2.ASGITransport(app=app))
        async with client:
            for call in ORACLE_CALLS.validate_python(oracle["calls"]):
                args = call["args"]
                assert isinstance(args, dict)
                ok, result = await client.call(str(call["tool"]), args)
                assert ok, result
            for tool, args in extra_calls:
                assert (await client.call(tool, args))[0]
        async with ControlClient(BASE, reg.control_token, transport=httpx.ASGITransport(app=app)) as control:
            world = await control.evidence()
    assert world.internal_error is None
    return world


async def test_oracle_calls_are_recorded_with_world_changes() -> None:
    world = await run_mini_mail(())
    assert len(world.final["mail.sent"]) == 1
    assert len([c for c in world.calls if c.tool == "mail_send"]) == 1
    assert set(world.diff.tables) == {"mail.sent"}
    mail_send = next(spec for spec in world.tool_schemas if spec.name == "mail_send")
    assert "body" in mail_send.input_schema["properties"]
    assert "attachments" not in mail_send.input_schema["properties"]


async def test_extra_calls_survive_the_evidence_round_trip() -> None:
    resend = ("mail_send", {"to": ["priya@northwind.test"], "body": "Vendor B, again.", "in_reply_to": "m1"})
    world = await run_mini_mail((("mail_archive", {"ids": ["m2"]}), resend))
    assert len(world.final["mail.sent"]) == 2
    assert "mail.messages" in world.diff.tables
    assert len([c for c in world.calls if c.tool == "mail_send"]) == 2


async def test_seed_clock_reaches_checks() -> None:
    """A year-less bound ("3 June") resolves against the world's seed clock, not the wall clock."""
    seed = WorldSeed.model_validate(
        {
            "now": "2026-06-03T09:00:00+01:00",
            "user": {"name": "Alex", "email": "alex@northwind.test", "timezone": "Europe/London"},
            "services": {"chat": {"channels": [{"id": "c1", "name": "general", "members": [], "topic": ""}]}},
        }
    )
    world = World(seed, ["mail", "chat"], [])
    ok, _ = await world.call("chat_post_message", {"channel": "#general", "text": "Standup moved to 10:00"})
    assert ok
    evidence = world.evidence()
    assert len(evidence.final["chat.messages"]) == 1
    assert evidence.calls[0].at == seed.now
    assert set(evidence.diff.tables) == {"chat.messages"}
