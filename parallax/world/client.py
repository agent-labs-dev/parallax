"""Clients for a running world server.

`WorldClient` is the agent-side MCP client (oracle, stdio bridge,
tests). `ControlClient` is the runner-side control API client.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Self

import httpx
import httpx2
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import JsonValue

from parallax.models import Fire, JsonObject, Task, WorldEvidence, WorldPatch
from parallax.world.server import AdminRegister, Registration
from parallax.world.service import ToolSpec

MCP_TIMEOUT = httpx2.Timeout(30.0, read=300.0)
CONTROL_TIMEOUT = 30.0


class WorldClient:
    """MCP client for one world.

    Use as `async with WorldClient(url, token) as c:` to hold one MCP session for many
    calls; outside a context each call opens (and closes) its own session.
    """

    def __init__(self, url: str, token: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        # `transport` lets tests drive an in-process ASGI app instead of a socket.
        self._url = url
        self._token = token
        self._transport = transport
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> Self:
        stack = contextlib.AsyncExitStack()
        try:
            self._session = await stack.enter_async_context(self._open())
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        async with self._use() as session:
            listed = await session.list_tools()
        return tuple(_spec(t) for t in listed.tools)

    async def call(self, tool: str, args: JsonObject) -> tuple[bool, JsonValue]:
        async with self._use() as session:
            result = await session.call_tool(tool, dict(args))
        if not isinstance(result, types.CallToolResult):
            raise RuntimeError(f"{tool}: world returned {type(result).__name__}, expected a tool result")
        return not result.is_error, _value(result)

    @contextlib.asynccontextmanager
    async def _use(self) -> AsyncGenerator[ClientSession]:
        if self._session is not None:
            yield self._session
            return
        async with self._open() as session:
            yield session

    @contextlib.asynccontextmanager
    async def _open(self) -> AsyncGenerator[ClientSession]:
        headers = {"Authorization": f"Bearer {self._token}"}
        http = httpx2.AsyncClient(
            headers=headers, transport=self._transport, timeout=MCP_TIMEOUT, follow_redirects=True
        )
        async with (
            http,
            streamable_http_client(self._url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session


def _spec(tool: types.Tool) -> ToolSpec:
    # The server publishes both classifications as MCP annotations; read them back rather than guess.
    hints = tool.annotations
    mutates = hints is None or hints.read_only_hint is not True
    additive = mutates and hints is not None and hints.destructive_hint is False
    return ToolSpec(name=tool.name, description=tool.description or "", input_schema=dict(tool.input_schema),
                    mutates=mutates, additive=additive)  # fmt: skip


def _value(result: types.CallToolResult) -> JsonValue:
    """The exact JSON value the world returned (the server always sends it as JSON text)."""
    if result.structured_content is not None:
        return dict(result.structured_content)
    texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
    if len(texts) != 1:
        raise ValueError(f"expected one text content block from the world, got {len(texts)}")
    return json.loads(texts[0])


class ControlClient:
    """Runner-side client for `/_control/{control_token}` (holding the token is the authorisation)."""

    def __init__(self, base_url: str, control_token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = f"{base_url.rstrip('/')}/_control/{control_token}"
        self._http = httpx.AsyncClient(transport=transport, timeout=CONTROL_TIMEOUT)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def position(self, turn: int, session: int) -> None:
        await self._post({"turn": turn, "session": session})

    async def patch(self, patch: WorldPatch) -> None:
        await self._post({"patch": patch.model_dump(mode="json")})

    async def fire(self, fire: Fire) -> JsonObject | None:
        body = await self._post({"fire": fire.model_dump(mode="json")})
        job = body.get("job")
        if job is not None and not isinstance(job, dict):
            raise ValueError(f"fire returned a non-object job: {job!r}")
        return job

    async def evidence(self) -> WorldEvidence:
        response = await self._http.get(f"{self._base}/evidence")
        _raise_for(response)
        return WorldEvidence.model_validate_json(response.content)

    async def unregister(self) -> None:
        _raise_for(await self._http.delete(self._base))

    async def _post(self, body: JsonObject) -> JsonObject:
        response = await self._http.post(self._base, json=body)
        _raise_for(response)
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"control API returned non-object {data!r}")
        return data


async def admin_register(
    base_url: str,
    admin_token: str,
    task: Task | str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Registration:
    """Register a task's world on a shared `python -m parallax.world serve --http` process: the loaded task,
    or the path of a task directory the server can read.

    Loaded tasks carry the host-resolved default-service decision because the sidecar has no
    plugins. Path registrations leave resolution to the server loading the task.
    """
    from parallax import extensions

    mount_defaults = extensions.default_services(task) if isinstance(task, Task) else None
    body = AdminRegister(task=task, mount_defaults=mount_defaults).model_dump(mode="json")
    async with httpx.AsyncClient(transport=transport, timeout=CONTROL_TIMEOUT) as http:
        response = await http.post(f"{base_url.rstrip('/')}/_admin", json=body,
                                   headers={"Authorization": f"Bearer {admin_token}"})  # fmt: skip
    _raise_for(response)
    return Registration.model_validate_json(response.content)


class ControlError(RuntimeError):
    """The world server refused a control request; the message carries its reason."""


def _raise_for(response: httpx.Response) -> None:
    if response.is_success:
        return
    # The URL is left out on purpose: it carries the control token.
    raise ControlError(f"{response.request.method} control request -> {response.status_code}: {response.text}")
