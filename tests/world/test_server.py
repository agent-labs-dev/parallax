"""World server over real MCP (in-process ASGI): tenancy, control API, round trips."""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import httpx
import httpx2
import pytest
from parallax import extensions
from parallax.extensions import TaskExtension
from parallax.models import Capability, Fire, Task, WorldPatch, WorldSeed
from parallax.task import load_task
from parallax.world.client import ControlClient, ControlError, WorldClient, admin_register
from parallax.world.core import World
from parallax.world.server import WorldServer
from parallax.world.service import Service, tool
from pydantic import BaseModel, JsonValue
from starlette.applications import Starlette

URL = "http://sidecar:7801/mcp"  # a non-localhost Host header must be accepted
BASE = "http://sidecar:7801"


class AddArgs(BaseModel):
    text: str


class CountArgs(BaseModel):
    pass


class Notes(Service):
    name = "notes"
    tables = ("items",)

    @tool(mutates=True, additive=True, description="Add a note.")
    async def add(self, args: AddArgs) -> JsonValue:
        note_id = self.world.new_id("n")
        self.world.table("notes.items")[note_id] = {"id": note_id, "text": args.text}
        return {"id": note_id}

    @tool(mutates=False, description="Count the notes.")
    async def count(self, args: CountArgs) -> JsonValue:
        return len(self.world.table("notes.items"))


class JobArgs(BaseModel):
    title: str


class Scheduler(Service):
    name = "scheduler"
    tables = ("jobs",)

    @tool(mutates=True, description="Create a job.")
    async def create_job(self, args: JobArgs) -> JsonValue:
        job_id = self.world.new_id("job")
        self.world.table("scheduler.jobs")[job_id] = {"id": job_id, "title": args.title}
        return {"id": job_id}


def make_world() -> World:
    seed = WorldSeed.model_validate(
        {"now": "2026-06-03T09:00:00+01:00", "user": {"name": "A", "email": "a@b.test", "timezone": "Europe/London"}}
    )
    return World(seed, ["notes", "scheduler"], [], registry={"notes": Notes, "scheduler": Scheduler})


@contextlib.asynccontextmanager
async def running(server: WorldServer) -> AsyncIterator[Starlette]:
    # Entered inside each test (not an async fixture): the MCP session manager's task
    # group must be exited by the task that entered it.
    application = server.app()
    async with application.router.lifespan_context(application):
        yield application


def mcp_client(app: Starlette, token: str) -> WorldClient:
    return WorldClient(URL, token, transport=httpx2.ASGITransport(app=app))


def control(app: Starlette, control_token: str) -> ControlClient:
    return ControlClient(BASE, control_token, transport=httpx.ASGITransport(app=app))


async def test_mcp_round_trip() -> None:
    server = WorldServer()
    async with running(server) as app:
        await _test_mcp_round_trip(server, app)


async def _test_mcp_round_trip(server: WorldServer, app: Starlette) -> None:
    world = make_world()
    token, _ = server.register(world)
    async with mcp_client(app, token) as client:
        # Each classification crosses MCP as an annotation and reads back unchanged.
        kinds = {spec.name: (spec.mutates, spec.additive) for spec in await client.list_tools()}
        assert kinds == {
            "notes_add": (True, True),
            "notes_count": (False, False),
            "scheduler_create_job": (True, False),
        }
        assert await client.call("notes_add", {"text": "hi"}) == (True, {"id": "n1"})
        ok, result = await client.call("notes_add", {})
        assert not ok and isinstance(result, dict) and result["error"]
    # Outside a context manager each call opens its own session.
    assert (await mcp_client(app, token).call("notes_add", {"text": "again"}))[0]
    assert [c.ok for c in world.evidence().calls] == [True, False, True]


async def test_each_app_path_serves_only_its_own_tools() -> None:
    server = WorldServer()
    async with running(server) as app:
        world = make_world()
        token, _ = server.register(world)
        notes = WorldClient(f"{URL}/notes", token, transport=httpx2.ASGITransport(app=app))
        async with notes as client:
            assert {spec.name for spec in await client.list_tools()} == {"notes_add", "notes_count"}
            assert await client.call("notes_add", {"text": "hi"}) == (True, {"id": "n1"})
            ok, result = await client.call("scheduler_create_job", {"title": "x"})
            assert not ok and isinstance(result, dict) and result["error"] == "not_found"
        # A refused cross-app call never reaches the world.
        assert [c.tool for c in world.evidence().calls] == ["notes_add"]
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as raw:
            missing = await raw.post(
                f"{URL}/mail",
                headers={"Authorization": f"Bearer {token}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert missing.status_code == 404


async def test_tokens_isolate_worlds() -> None:
    server = WorldServer()
    async with running(server) as app:
        await _test_tokens_isolate_worlds(server, app)


async def _test_tokens_isolate_worlds(server: WorldServer, app: Starlette) -> None:
    first, second = make_world(), make_world()
    token_a, _ = server.register(first)
    server.register(second)
    await mcp_client(app, token_a).call("notes_add", {"text": "only in a"})
    assert len(first.evidence().calls) == 1
    assert second.evidence().calls == ()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as raw:
        for headers in ({"Authorization": "Bearer wrong"}, {}):
            response = await raw.post(URL, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert response.status_code == 401


async def test_control_position_stamps_calls() -> None:
    server = WorldServer()
    async with running(server) as app:
        await _test_control_position_stamps_calls(server, app)


async def _test_control_position_stamps_calls(server: WorldServer, app: Starlette) -> None:
    world = make_world()
    token, control_token = server.register(world)
    async with control(app, control_token) as ctl, mcp_client(app, token) as client:
        await ctl.position(2, 1)
        await client.call("notes_add", {"text": "a"})
        await ctl.position(3, 2)
        await client.call("notes_add", {"text": "b"})
        evidence = await ctl.evidence()
    assert [(c.turn, c.session, c.at.day) for c in evidence.calls] == [(2, 1, 3), (3, 2, 3)]


async def test_control_patch_fire_and_unregister() -> None:
    server = WorldServer()
    async with running(server) as app:
        await _test_control_patch_fire_and_unregister(server, app)


async def _test_control_patch_fire_and_unregister(server: WorldServer, app: Starlette) -> None:
    world = make_world()
    token, control_token = server.register(world)
    await mcp_client(app, token).call("scheduler_create_job", {"title": "Digest"})
    at = datetime.fromisoformat("2026-06-05T08:00:00+01:00")
    async with control(app, control_token) as ctl:
        await ctl.patch(WorldPatch(insert={"notes.items": [{"id": "p1", "text": "arrived"}]}))
        assert await ctl.fire(Fire(name="fire-1", at=at, job={"title": "digest"})) == {"id": "job1", "title": "Digest"}
        assert await ctl.fire(Fire(name="fire-2", at=at, job={"title": "none"})) is None
        evidence = await ctl.evidence()
        assert "p1" in evidence.final["notes.items"] and evidence.missed_fires == ("fire-2",)
        await ctl.unregister()
        with pytest.raises(ControlError, match="404"):
            await ctl.evidence()
    assert server.world_for(token) is None


async def test_admin_endpoint_requires_the_admin_token_and_a_loadable_task(tmp_path: Path) -> None:
    def no_task(path: Path) -> Task:
        raise ValueError(f"no task at {path}")

    server = WorldServer(admin_token="admin-secret", load_task=no_task)
    transport = httpx.ASGITransport(app=server.app())
    with pytest.raises(ControlError, match="401"):
        await admin_register(BASE, "wrong", str(tmp_path), transport=transport)
    with pytest.raises(ControlError, match="400"):
        await admin_register(BASE, "admin-secret", str(tmp_path), transport=transport)
    closed = httpx.ASGITransport(app=WorldServer().app())
    with pytest.raises(ControlError, match="401"):
        await admin_register(BASE, "anything", str(tmp_path), transport=closed)


class _FakeBlock(BaseModel):
    level: int


TASKS = Path(__file__).parents[1] / "core" / "fixtures" / "tasks"


class _SidecarTransport(httpx.AsyncBaseTransport):
    """Flips the plugin registry to empty just before the sidecar handles the request.

    The docker sidecar's image holds core without the host's plugins, so a task whose
    metadata names a plugin extension must be registered by the decision the host carried,
    not by re-indexing a registry the sidecar does not have.
    """

    def __init__(self, app: Starlette, monkeypatch: pytest.MonkeyPatch) -> None:
        self._inner = httpx.ASGITransport(app=app)
        self._monkeypatch = monkeypatch

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._monkeypatch.setattr(extensions, "task_extensions", lambda: {})
        return await self._inner.handle_async_request(request)


@pytest.mark.parametrize(("defaults", "mcp"), [(False, True), (True, True), (True, False)])
async def test_admin_registration_carries_the_host_service_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defaults: bool, mcp: bool
) -> None:
    """The host's extension decision crosses the HTTP boundary; MCP remains mandatory for defaults."""
    root = tmp_path / "sidecar-task"
    shutil.copytree(TASKS / "mini-mail", root)
    (root / "task.toml").write_text(
        (root / "task.toml").read_text() + "\nservices = ['mail']\n\n[metadata.fake]\nlevel = 1\n"
    )
    # The seed must only reference services that stay mounted once the defaults are excluded.
    (root / "world" / "seed.yaml").write_text(
        "now: 2026-06-03T09:12:00+01:00\n"
        "user: {name: Alex Morgan, email: alex@northwind.test, timezone: Europe/London}\n"
        "mail:\n"
        "  messages:\n"
        "    - {id: m1, thread: t1, from: priya@northwind.test, to: [alex@northwind.test], cc: [],\n"
        '       subject: "Hi", body: "Hello", date: "2026-06-02T16:40:00+01:00", labels: [inbox]}\n'
    )
    fake = TaskExtension(name="fake", model=_FakeBlock, default_services=defaults)
    monkeypatch.setattr(extensions, "task_extensions", lambda: {"fake": fake})

    task = load_task(root)
    assert task.extensions == {"fake": {"level": 1}}
    assert extensions.default_services(task) is defaults
    if not mcp:
        task = task.model_copy(update={"meta": task.meta.model_copy(update={"requires": frozenset()})})
    assert (Capability.MCP in task.meta.requires) is mcp

    server = WorldServer(admin_token="admin-secret")
    async with running(server) as app:
        reg = await admin_register(BASE, "admin-secret", task, transport=_SidecarTransport(app, monkeypatch))
        tools = await mcp_client(app, reg.token).list_tools()
    names = {spec.name for spec in tools}
    assert "mail_send" in names  # the task's explicitly declared service
    assert ("chat_post_message" in names) is (defaults and mcp)
    assert ("user_whoami" in names) is (defaults and mcp)
