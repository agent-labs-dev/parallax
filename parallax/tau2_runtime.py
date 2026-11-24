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
        self.simulator_cost_usd: float | None = None
        self.tools: list[dict[str, Any]] = []
        self.max_steps: int | None = None
        self.max_errors: int | None = None
        self.tool_errors = 0

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise Tau2RuntimeError("tau2 serve process is not running")
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
        return await self._request({"op": "tool", "name": name, "arguments": arguments, "id": call_id})

    async def user_turn(self, *, first: bool, assistant: str = "") -> dict[str, Any]:
        result = await self._request({"op": "user", "first": first, "assistant": assistant})
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
        self.errors = 0
        self._counter = 0

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        self._counter += 1
        call_id = f"call-{self._counter}"
        try:
            result = await self.runtime.call_tool(name, arguments, call_id)
            content, error = str(result.get("content") or ""), bool(result.get("error"))
        except Tau2RuntimeError as exc:
            content, error = f"Error: {exc}", True
        if error:
            self.errors += 1
        self.entries.append({"id": call_id, "name": name, "arguments": arguments, "content": content, "error": error})
        return content, error


def tau2_mcp_app(log: Tau2ToolLog) -> Starlette:
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

    return Starlette(routes=[Route("/mcp", endpoint=Endpoint()), Route("/", endpoint=not_found)], lifespan=lifespan)
