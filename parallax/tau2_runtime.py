"""Parallax-owned tau2 trial runtime: the domain tool server and the user simulator.

Per trial, parallax starts `tau2_bridge.py` in serve mode under the pinned tau2 checkout's
Python (a subprocess like the grading bridge). That process holds the domain environment and
the upstream user simulator built from the task's own scenario; parallax serves its tools to
the harness as the trial's world MCP server, drives the multi-turn loop, and records the
transcript itself from this server's tool log and the turns. Simulator cost is tracked apart
from harness usage. The scenario never enters the agent prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from parallax.models import JsonObject
from parallax.world.service import ToolSpec

AGENT_STOP = "###STOP###"


class Tau2RuntimeError(RuntimeError):
    """The tau2 serve process cannot start or answer."""


class Tau2BridgeRuntime:
    """A serve-mode bridge subprocess for one trial."""

    def __init__(
        self, *, root: Path, python: Path, commit: str, base_url: str, api_key: str, simulator_model: str = ""
    ) -> None:
        self._root = root
        self._python = python
        self._commit = commit
        self._base_url = base_url
        self._api_key = api_key
        self._simulator_model = simulator_model
        self._process: asyncio.subprocess.Process | None = None
        # One request in flight: the serve process answers on a single stdout, one line per request.
        self._io = asyncio.Lock()
        self.simulator_cost_usd: float | None = None
        self.tools: list[dict[str, Any]] = []
        self.max_steps: int | None = None
        self.max_errors: int | None = None
        self.tool_errors = 0

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise Tau2RuntimeError("tau2 serve process is not running")
        async with self._io:
            process.stdin.write((json.dumps(payload) + "\n").encode())
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), timeout=900.0)
        if not line:
            raise Tau2RuntimeError("tau2 serve process ended")
        result = json.loads(line)
        if not isinstance(result, dict) or not result.get("ok"):
            raise Tau2RuntimeError(f"tau2 serve process failed: {(result or {}).get('error', 'unknown')}")
        return result

    async def start(self, *, domain: str, task: JsonObject) -> list[dict[str, Any]]:
        self.tool_errors = 0
        command = (str(self._python), "-B", str(Path(__file__).with_name("tau2_bridge.py")))
        env = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "OPENAI_BASE_URL": self._base_url,
            "OPENAI_API_KEY": self._api_key,
        }
        self._process = await asyncio.create_subprocess_exec(
            *command, cwd=self._root, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )  # fmt: skip
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(
            (json.dumps({"operation": "serve", "root": str(self._root), "commit": self._commit}) + "\n").encode()
        )
        await process.stdin.drain()
        line = await asyncio.wait_for(process.stdout.readline(), timeout=120.0)
        if not line or not json.loads(line).get("ok"):
            raise Tau2RuntimeError("tau2 serve process did not start")
        result = await self._request(
            {"op": "start", "domain": domain, "task": task, "simulator_model": self._simulator_model}
        )
        self.tools = list(result.get("tools") or [])
        self.max_steps = int(result["max_steps"])
        self.max_errors = int(result["max_errors"])
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        result = await self._request({"op": "tool", "name": name, "arguments": arguments, "id": call_id})
        self.tool_errors = int(result.get("tool_errors", self.tool_errors))
        return result

    async def user_turn(
        self, *, first: bool, assistant: str = "", remaining_steps: int | None = None
    ) -> dict[str, Any]:
        result = await self._request(
            {"op": "user", "first": first, "assistant": assistant, "remaining_steps": remaining_steps}
        )
        cost = result.get("simulator_cost_usd")
        self.simulator_cost_usd = float(cost) if isinstance(cost, (int, float)) else self.simulator_cost_usd
        self.tool_errors = int(result.get("tool_errors", self.tool_errors))
        return result

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            await self._request_close(process)
        except (TimeoutError, Tau2RuntimeError, OSError):
            process.kill()
        await process.wait()

    @staticmethod
    async def _request_close(process: asyncio.subprocess.Process) -> None:
        if process.stdin is None:
            return
        process.stdin.write(b'{"op": "close"}\n')
        await process.stdin.drain()
        process.stdin.close()


class Tau2ToolLog:
    """Tool calls the trial's MCP server served, in order: the transcript's tool evidence."""

    def __init__(self, runtime: Tau2BridgeRuntime) -> None:
        self.runtime = runtime
        self.entries: list[JsonObject] = []
        self._counter = 0
        self._batch_counter = 0
        self._active_batch: int | None = None
        self._active_calls = 0

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        self._counter += 1
        call_id = f"call-{self._counter}"
        if self._active_calls == 0:
            self._batch_counter += 1
            self._active_batch = self._batch_counter
        batch = self._active_batch
        self._active_calls += 1
        try:
            result = await self.runtime.call_tool(name, arguments, call_id)
            content, error = str(result.get("content") or ""), bool(result.get("error"))
        except Tau2RuntimeError as exc:
            content, error = f"Error: {exc}", True
        finally:
            self._active_calls -= 1
            if self._active_calls == 0:
                self._active_batch = None
        self.entries.append(
            {
                "id": call_id,
                "name": name,
                "arguments": arguments,
                "content": content,
                "error": error,
                "batch": batch,
            }
        )
        return content, error


class Tau2Backend:
    """Expose one tau2 tool log through the shared world-server auth boundary."""

    def __init__(self, log: Tau2ToolLog) -> None:
        self.log = log

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(
                name=str(tool["name"]),
                description=str(tool.get("description") or tool["name"]),
                input_schema=dict(tool.get("parameters") or {"type": "object", "properties": {}}),
                mutates=True,
                additive=False,
            )
            for tool in self.log.runtime.tools
        )

    async def call(self, tool: str, args: JsonObject) -> tuple[bool, str]:
        content, error = await self.log.call(tool, dict(args))
        return not error, content


def tau2_mcp_app(log: Tau2ToolLog, *, token: str | None = None, nonce: str | None = None) -> Starlette:
    """The trial's world MCP server: the domain tools, served by parallax on localhost."""

    async def on_list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=str(tool["name"]),
                    description=str(tool.get("description") or tool["name"]),
                    input_schema=dict(tool.get("parameters") or {"type": "object", "properties": {}}),
                )
                for tool in log.runtime.tools
            ]
        )

    async def on_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        content, error = await log.call(params.name, dict(params.arguments or {}))
        return types.CallToolResult(content=[types.TextContent(type="text", text=content)], is_error=error)

    server = Server("parallax-tau2", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    manager = StreamableHTTPSessionManager(app=server, json_response=True)

    class Endpoint:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with manager.run():
            yield

    async def not_found(_request: Request) -> JSONResponse:
        return JSONResponse({"error": "tau2 tool server"}, status_code=404)

    async def identity(request: Request) -> JSONResponse:
        expected = f"Bearer {token}" if token is not None else None
        if expected is None or request.headers.get("authorization") != expected:
            return JSONResponse({"error": "unknown or missing bearer token"}, status_code=401)
        return JSONResponse({"nonce": nonce})

    async def authenticated(scope: Scope, receive: Receive, send: Send) -> None:
        expected = f"Bearer {token}" if token is not None else None
        request = Request(scope)
        if expected is None or not secrets.compare_digest(request.headers.get("authorization", ""), expected):
            await JSONResponse({"error": "unknown or missing bearer token"}, status_code=401)(scope, receive, send)
            return
        await Endpoint()(scope, receive, send)

    return Starlette(
        routes=[
            Route("/_identity", endpoint=identity, methods=["GET"]),
            Route("/mcp", endpoint=authenticated),
            Route("/", endpoint=not_found),
        ],
        lifespan=lifespan,
    )
