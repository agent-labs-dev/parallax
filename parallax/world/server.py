"""World server: MCP over streamable HTTP and stdio, plus the runner's control API.

One process serves every trial of a run. Each registered world gets two fresh
32-byte tokens: the MCP bearer token the harness holds, and the control token only
the runner holds. The MCP layer is a thin relay: tool names, schemas and results are
exactly the world's, and every call lands in the world's own log.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sys
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import JsonValue, TypeAdapter, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from parallax.models import Fire, JsonObject, Model, Task, WorldPatch
from parallax.world.core import World, WorldError
from parallax.world.service import ToolSpec

MCP_PATH = "/mcp"
SERVER_NAME = "parallax-world"
ADMIN_TOKEN_ENV = "PARALLAX_WORLD_ADMIN_TOKEN"


class ToolBackend(Protocol):
    """What the MCP relay serves: a local World, or (for the stdio bridge) a remote one."""

    async def list_tools(self) -> tuple[ToolSpec, ...]: ...

    async def call(self, tool: str, args: JsonObject) -> tuple[bool, JsonValue]: ...


@dataclass(frozen=True)
class LocalBackend:
    world: World
    service: str | None = None  # serve only this service's tools (`<MCP_PATH>/<service>`), as one app

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(s for s in self.world.specs() if self._serves(s.name))

    async def call(self, tool: str, args: JsonObject) -> tuple[bool, JsonValue]:
        if not self._serves(tool):
            # Another app's tool is not on this server, as with a real one; the world never sees the call.
            return False, {"error": "not_found", "message": f"unknown tool {tool}"}
        return await self.world.call(tool, args)

    def _serves(self, tool: str) -> bool:
        return self.service is None or tool.startswith(f"{self.service}_")


# ---- control API bodies ------------------------------------------------------------------


class PositionBody(Model):
    turn: int
    session: int


class PatchBody(Model):
    patch: WorldPatch


class FireBody(Model):
    fire: Fire


ControlBody = PositionBody | PatchBody | FireBody
_CONTROL_BODY: TypeAdapter[ControlBody] = TypeAdapter(ControlBody)


class AdminRegister(Model):
    # The loaded task travels whole whenever the server may not share the runner's filesystem
    # (the docker sidecar); a path is for a server on the same host, which loads it itself.
    task: Task | str
    # Host-resolved extension defaults for plugin-less sidecars; None resolves locally.
    mount_defaults: bool | None = None


class Registration(Model):
    token: str
    control_token: str


# ---- MCP relay ---------------------------------------------------------------------------

ResolveBackend = Callable[[ServerRequestContext], ToolBackend]


def build_mcp_server(resolve: ResolveBackend) -> Server:
    async def on_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        specs = await resolve(ctx).list_tools()
        return types.ListToolsResult(tools=[_mcp_tool(s) for s in specs])

    async def on_call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        ok, result = await resolve(ctx).call(params.name, dict(params.arguments or {}))
        return tool_result(ok, result)

    return Server(SERVER_NAME, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def _mcp_tool(spec: ToolSpec) -> types.Tool:
    annotations = types.ToolAnnotations(
        read_only_hint=not spec.mutates, destructive_hint=spec.mutates and not spec.additive
    )
    return types.Tool(name=spec.name, description=spec.description, input_schema=spec.input_schema,
                      annotations=annotations)  # fmt: skip


def tool_result(ok: bool, result: JsonValue) -> types.CallToolResult:
    # Text content for every client; structured content too when the result is an object,
    # which is what lets WorldClient recover the exact JSON value.
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
        structured_content=result if isinstance(result, dict) else None,
        is_error=not ok,
    )


def bearer(headers: Mapping[str, str]) -> str | None:
    value = headers.get("authorization")
    if value is None:
        return None
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


# ---- multi-tenant server -----------------------------------------------------------------


@dataclass(frozen=True)
class _Tenant:
    world: World
    token: str
    control_token: str


class WorldServer:
    def __init__(
        self,
        *,
        admin_token: str | None = None,
        load_task: Callable[[Path], Task] | None = None,
    ) -> None:
        # `load_task` is injectable so the admin endpoint is testable without the task
        # loader; the default is resolved lazily for the same reason.
        self._admin_token = admin_token
        self._load_task = load_task
        self._by_token: dict[str, _Tenant] = {}
        self._by_control: dict[str, _Tenant] = {}
        self._mcp = build_mcp_server(self._resolve)

    def register(self, world: World) -> tuple[str, str]:
        tenant = _Tenant(world=world, token=secrets.token_urlsafe(32), control_token=secrets.token_urlsafe(32))
        self._by_token[tenant.token] = tenant
        self._by_control[tenant.control_token] = tenant
        return tenant.token, tenant.control_token

    def unregister(self, control_token: str) -> None:
        tenant = self._by_control.pop(control_token, None)
        if tenant is None:
            raise KeyError("unknown control token")
        del self._by_token[tenant.token]

    def world_for(self, token: str) -> World | None:
        tenant = self._by_token.get(token)
        return tenant.world if tenant else None

    def app(self) -> Starlette:
        # No DNS-rebinding protection: the harness reaches this server under hostnames
        # like `sidecar`, and every request is already authorised by a per-trial token.
        manager = StreamableHTTPSessionManager(app=self._mcp, json_response=True)

        @contextlib.asynccontextmanager
        async def lifespan(_: Starlette) -> AsyncGenerator[None]:
            async with manager.run():
                yield

        routes = [
            Route("/_admin", self._admin, methods=["POST"]),
            Route("/_control/{control_token}", self._control, methods=["POST", "DELETE"]),
            Route("/_control/{control_token}/evidence", self._evidence, methods=["GET"]),
            Route(MCP_PATH, endpoint=_McpEndpoint(self, manager)),
            Route(MCP_PATH + "/{service}", endpoint=_McpEndpoint(self, manager)),
        ]
        return Starlette(routes=routes, lifespan=lifespan)

    def _resolve(self, ctx: ServerRequestContext) -> ToolBackend:
        request = ctx.request
        token = bearer(request.headers) if isinstance(request, Request) else None
        world = self.world_for(token) if token else None
        if world is None:
            # The HTTP layer already rejected unknown tokens; reaching here means a tenant
            # was unregistered mid-session, which must not fall through to another world.
            raise PermissionError("world unregistered")
        return LocalBackend(world, app_service(request.url.path) if isinstance(request, Request) else None)

    async def _admin(self, request: Request) -> Response:
        if self._admin_token is None or not secrets.compare_digest(bearer(request.headers) or "", self._admin_token):
            return JSONResponse({"error": "admin token required"}, status_code=401)
        try:
            body = AdminRegister.model_validate_json(await request.body())
            task = body.task if isinstance(body.task, Task) else self._task_loader()(Path(body.task))
            world = World.for_task(task, mount_defaults=body.mount_defaults)
        except (ValidationError, WorldError, OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        token, control_token = self.register(world)
        return JSONResponse(Registration(token=token, control_token=control_token).model_dump())

    def _task_loader(self) -> Callable[[Path], Task]:
        if self._load_task is not None:
            return self._load_task
        from parallax.task import load_task

        return load_task

    async def _control(self, request: Request) -> Response:
        tenant = self._by_control.get(request.path_params["control_token"])
        if tenant is None:
            return JSONResponse({"error": "unknown control token"}, status_code=404)
        if request.method == "DELETE":
            self.unregister(tenant.control_token)
            return JSONResponse({"ok": True})
        try:
            body = _CONTROL_BODY.validate_json(await request.body())
            return JSONResponse(apply_control(tenant.world, body))
        except (ValidationError, WorldError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def _evidence(self, request: Request) -> Response:
        tenant = self._by_control.get(request.path_params["control_token"])
        if tenant is None:
            return JSONResponse({"error": "unknown control token"}, status_code=404)
        return Response(tenant.world.evidence().model_dump_json(), media_type="application/json")


class _McpEndpoint:
    # A class instance (not a function) so Starlette's Route treats it as a raw ASGI app,
    # which the streamable-HTTP transport needs for its streaming responses.
    def __init__(self, server: WorldServer, manager: StreamableHTTPSessionManager) -> None:
        self._server = server
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        token = bearer(request.headers)
        world = self._server.world_for(token) if token is not None else None
        if world is None:
            await JSONResponse({"error": "unknown or missing bearer token"}, status_code=401)(scope, receive, send)
            return
        service = app_service(request.url.path)
        if service is not None and service not in world.services():
            await JSONResponse({"error": f"no app {service!r} in this world"}, status_code=404)(scope, receive, send)
            return
        await self._manager.handle_request(scope, receive, send)


def app_service(path: str) -> str | None:
    """The service of a per-app MCP path (`/mcp/mail` -> mail); None for the whole world at `/mcp`."""
    tail = path.rstrip("/").removeprefix(MCP_PATH)
    return (tail.removeprefix("/") or None) if tail.startswith("/") else None


def apply_control(world: World, body: ControlBody) -> JsonObject:
    match body:
        case PositionBody(turn=turn, session=session):
            world.set_position(turn, session)
        case PatchBody(patch=patch):
            world.apply_patch(patch)
        case FireBody(fire=fire):
            return {"job": world.fire(fire)}
    return {"ok": True}


# ---- stdio, serving and the bridge -------------------------------------------------------


async def serve_backend_stdio(backend: ToolBackend) -> None:
    server = build_mcp_server(lambda _ctx: backend)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


async def serve_stdio(world: World) -> None:
    """Single-world stdio server (debug mode; no token, the pipe is the authorisation)."""
    await serve_backend_stdio(LocalBackend(world))


async def serve_http(server: WorldServer, host: str, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(server.app(), host=host, port=port, log_level="warning", lifespan="on")
    await uvicorn.Server(config).serve()


def bridge_main(url: str, token_env: str) -> int:
    """stdio <-> streamable-HTTP relay for harnesses that only speak stdio MCP."""
    import anyio

    from parallax.world.client import WorldClient

    token = os.environ.get(token_env)
    if not token:
        print(f"parallax world bridge: ${token_env} is not set", file=sys.stderr)
        return 2

    async def relay() -> None:
        async with WorldClient(url, token) as client:
            await serve_backend_stdio(client)

    anyio.run(relay)
    return 0
