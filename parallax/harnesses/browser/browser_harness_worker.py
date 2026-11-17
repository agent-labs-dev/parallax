"""JSON-line bridge that runs a browser CLI and its skill (browser-harness, agent-browser) under a coding agent.

The agent (Codex or Claude Code) gets the skill where it natively looks for skills, the CLI on PATH and the cloud
browser's CDP URL in the environment (`BU_CDP_WS` for browser-harness, `AGENT_BROWSER_CDP` for agent-browser). Its model is the trial's OpenRouter model id. The agent reaches
OpenRouter through a relay in this worker, which adds up the cost OpenRouter reports on every response: OpenRouter
bills long prompts and cache writes at their own rates, and neither CLI reports tokens per request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, assert_never

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

OPENROUTER = "https://openrouter.ai"
AGENT_COMPONENT = "agent model"
# What the relay forwards: the agent's credentials and the request's encoding, never hop-by-hop headers.
FORWARDED_HEADERS = ("authorization", "x-api-key", "content-type", "accept", "anthropic-version", "anthropic-beta")


class Wire(StrEnum):
    """The generation API an agent calls: Codex uses OpenAI Responses, Claude Code Anthropic Messages."""

    RESPONSES = "responses"
    MESSAGES = "messages"


class Agent(StrEnum):
    CODEX = "codex"
    CLAUDE_CODE = "claude-code"

    def skill_dir(self, name: str) -> Path:
        """Where the agent reads the skill called `name` relative to its working directory, per its own docs."""
        match self:
            case Agent.CODEX:
                return Path(".agents/skills") / name
            case Agent.CLAUDE_CODE:
                return Path(".claude/skills") / name
            case _:
                assert_never(self)


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


@dataclass(frozen=True)
class Billed:
    """One generation as OpenRouter billed it. `input` counts every input token, cached ones included."""

    model: str
    cost_usd: float | None
    input: int
    output: int
    cache_read: int
    cache_write: int
    reasoning: int

    def row(self) -> dict[str, Any]:
        return {
            "model": self.model, "input": self.input, "output": self.output, "cache_read": self.cache_read,
            "cache_write": self.cache_write, "reasoning": self.reasoning, "requests": 1, "cost_usd": self.cost_usd,
        }  # fmt: skip


def billed(body: bytes, model: str, wire: Wire) -> Billed:
    """A generation's usage from its JSON body or its SSE stream: the last usage object, which on both wires is the
    one OpenRouter adds its `cost` to (`response.completed` on Responses, `message_delta` on Messages)."""
    text = body.decode(errors="replace")
    payloads = [line[len("data:") :].strip() for line in text.splitlines() if line.startswith("data:")] or [text]
    usage: dict[str, Any] = {}
    for payload in payloads:
        try:
            event = _object(json.loads(payload))
        except json.JSONDecodeError:
            continue
        found = _object(event.get("usage")) or _object(_object(event.get("response")).get("usage"))
        if found:
            usage = found
    cost = usage.get("cost")
    match wire:
        case Wire.RESPONSES:
            # input_tokens already counts the cached ones.
            cache_read, cache_write = int(_object(usage.get("input_tokens_details")).get("cached_tokens") or 0), 0
            total_input = int(usage.get("input_tokens") or 0)
            reasoning = int(_object(usage.get("output_tokens_details")).get("reasoning_tokens") or 0)
        case Wire.MESSAGES:
            # input_tokens counts only what was neither read from nor written to the cache.
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_write = int(usage.get("cache_creation_input_tokens") or 0)
            total_input = int(usage.get("input_tokens") or 0) + cache_read + cache_write
            reasoning = int(_object(usage.get("output_tokens_details")).get("thinking_tokens") or 0)
        case _:
            assert_never(wire)
    return Billed(
        model=model,
        # A generation with no reported cost is unpriced, never free.
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        input=total_input,
        output=int(usage.get("output_tokens") or 0),
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=reasoning,
    )


@dataclass
class Relay:
    """Forwards the agent's model calls to OpenRouter and keeps what each generation was billed."""

    upstream: str = OPENROUTER
    bills: list[Billed] = field(default_factory=list)

    async def forward(self, request: Request) -> StreamingResponse:
        body = await request.body()
        headers = {name: value for name, value in request.headers.items() if name.lower() in FORWARDED_HEADERS}
        http = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30))
        upstream = await http.send(
            http.build_request(request.method, f"{self.upstream}{request.url.path}", params=request.query_params,
                               headers=headers, content=body),
            stream=True,
        )  # fmt: skip
        model = str(_object(json.loads(body)).get("model", "unreported")) if body else "unreported"
        wire = next((w for w in Wire if request.url.path.endswith(f"/{w}")), None) if request.method == "POST" else None

        async def stream() -> AsyncIterator[bytes]:
            received = bytearray()
            try:
                # Decoded: the agent is told the upstream content type, never its compression.
                async for chunk in upstream.aiter_bytes():
                    received.extend(chunk)
                    yield chunk
            finally:
                await upstream.aclose()
                await http.aclose()
                if wire is not None and upstream.is_success:
                    self.bills.append(billed(bytes(received), model, wire))

        content_type = upstream.headers.get("content-type", "application/json")
        return StreamingResponse(stream(), status_code=upstream.status_code, media_type=content_type)

    @asynccontextmanager
    async def serving(self) -> AsyncGenerator[str]:
        """The relay's base URL while it serves on a free local port."""
        app = Starlette(routes=[Route("/{path:path}", self.forward, methods=["GET", "POST"])])
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                task.result()
                raise RuntimeError("model relay stopped before it served")
            await asyncio.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await task

    def report(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Usage rows and the one agent-model cost line, unknown when any generation's cost is."""
        # No generation reached the relay: whatever the agent spent went around it, so its cost is unknown.
        total: float | None = 0.0 if self.bills else None
        for bill in self.bills:
            total = None if bill.cost_usd is None or total is None else total + bill.cost_usd
        return [bill.row() for bill in self.bills], [{"component": AGENT_COMPONENT, "dollars": total}]


@dataclass
class Run:
    answer: str | None = None
    finished: bool = False
    error: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)


def argv(agent: Agent, agent_bin: str, model: str, goal: str, relay: str) -> list[str]:
    match agent:
        case Agent.CODEX:
            provider = "model_providers.openrouter"
            return [
                agent_bin, "exec", "--json", "--skip-git-repo-check",
                # The browser is reached over the network, which Codex's sandbox blocks; the runner is disposable.
                "--sandbox", "danger-full-access",
                "-c", "model_provider=openrouter",
                "-c", f'{provider}.name="openrouter"',
                "-c", f'{provider}.base_url="{relay}/api/v1"',
                "-c", f'{provider}.env_key="OPENROUTER_API_KEY"',
                # The browser is the instrument measured: no search tool to answer around it.
                "-c", 'web_search="disabled"',
                # The CLI reaches its browser through environment variables, which the shell tool must pass on.
                "-c", 'shell_environment_policy.inherit="all"',
                "--model", model, "--", goal,
            ]  # fmt: skip
        case Agent.CLAUDE_CODE:
            return [
                agent_bin, "-p", "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions",
                "--disallowedTools", "WebSearch", "WebFetch",
                "--model", model, "--", goal,
            ]  # fmt: skip
        case _:
            assert_never(agent)


def agent_env(agent: Agent, model: str, relay: str, base: dict[str, str], home: Path) -> dict[str, str]:
    key = base.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("a browser CLI harness needs OPENROUTER_API_KEY for its agent's model")
    match agent:
        case Agent.CODEX:
            # Its own home, not the host user's: their config.toml would set the shell's environment, MCP servers
            # and hooks, so a trial would measure the machine it happens to run on.
            return base | {"CODEX_HOME": str(home)}
        case Agent.CLAUDE_CODE:
            # OpenRouter's documented Claude Code setup; every model alias, background work included, is the trial's.
            return base | {
                "ANTHROPIC_BASE_URL": f"{relay}/api",
                "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "CLAUDE_CODE_SUBAGENT_MODEL": model,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        case _:
            assert_never(agent)


def _events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def parse_codex(stdout: str) -> Run:
    """`codex exec --json`: the last agent message answers, and each shell command is a step."""
    run = Run()
    for event in _events(stdout):
        item = _object(event.get("item"))
        match event.get("type"):
            case "item.completed" if item.get("type") == "agent_message":
                run.answer = str(item.get("text", ""))
            case "item.completed" if item.get("type") == "command_execution":
                run.steps.append(
                    {
                        "kind": "tool_call",
                        "name": "shell",
                        "args": item.get("command"),
                        "output": item.get("aggregated_output"),
                        "ok": item.get("exit_code") == 0,
                    }
                )
            case "turn.completed":
                run.finished = True
            case "turn.failed" | "error":
                error = event.get("error")
                run.error = str(_object(error).get("message") or event.get("message") or error)
            case _:
                pass
    return run


def parse_claude(stdout: str) -> Run:
    """`claude -p --output-format stream-json`: tool calls from assistant messages, the answer from the result."""
    run = Run()
    calls: dict[str, dict[str, Any]] = {}
    for event in _events(stdout):
        content = _items(_object(event.get("message")).get("content"))
        match event.get("type"):
            case "assistant":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        step = {"kind": "tool_call", "name": block.get("name"), "args": block.get("input")}
                        calls[str(block.get("id"))] = step
                        run.steps.append(step)
            case "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        step = calls.get(str(block.get("tool_use_id")))
                        if step is not None:
                            step["output"] = block.get("content")
                            step["ok"] = not block.get("is_error", False)
            case "result":
                run.finished = event.get("subtype") == "success" and not event.get("is_error", False)
                run.answer = event.get("result") if isinstance(event.get("result"), str) else None
                if not run.finished:
                    run.error = str(event.get("result") or event.get("subtype") or "claude-code failed")
            case _:
                pass
    return run


def parse(agent: Agent, stdout: str) -> Run:
    match agent:
        case Agent.CODEX:
            return parse_codex(stdout)
        case Agent.CLAUDE_CODE:
            return parse_claude(stdout)
        case _:
            assert_never(agent)


def install_skill(agent: Agent, cwd: Path, skill: Path, name: str) -> None:
    target = cwd / agent.skill_dir(name)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(skill, target / "SKILL.md")


async def run(request: dict[str, Any]) -> None:
    # The host ends an interrupted trial with SIGTERM, which must still stop the CLI's background process.
    task = asyncio.current_task()
    if task is not None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    # The pinned CLI comes first on the agent's PATH.
    environ = os.environ | {"PATH": os.pathsep.join((str(request["bin_dir"]), os.environ.get("PATH", "")))}
    detach = Detach(_argv(request.get("detach")), environ, Path.cwd())
    try:
        await _run(request, environ, detach)
    finally:
        await asyncio.shield(detach())


@dataclass
class Detach:
    """The command that ends the CLI's own background process, run once."""

    command: list[str]
    env: dict[str, str]
    cwd: Path
    done: bool = False

    async def __call__(self) -> None:
        if self.command and not self.done:
            self.done = True
            with contextlib.suppress(OSError, RuntimeError):
                await _cli(self.command, self.env, self.cwd)


async def _run(request: dict[str, Any], environ: dict[str, str], detach: Detach) -> None:
    agent, model, goal = Agent(request["agent"]), str(request["model"]), str(request["goal"])
    cwd, relay = Path.cwd(), Relay()
    try:
        install_skill(agent, cwd, Path(request["skill"]), str(request.get("skill_name", "browser-harness")))
        (cwd / ".codex-home").mkdir(exist_ok=True)
        async with relay.serving() as relay_url:
            process = await asyncio.create_subprocess_exec(
                *argv(agent, str(request["agent_bin"]), model, goal, relay_url),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=agent_env(agent, model, relay_url, environ, cwd / ".codex-home"),
                cwd=cwd,
            )
            stdout, stderr = await process.communicate()
    except (OSError, RuntimeError) as error:
        usage, cost_lines = relay.report()
        emit(
            {
                "event": "result", "status": "error", "answer": None, "error": f"{type(error).__name__}: {error}",
                "usage": usage, "cost_lines": cost_lines if relay.bills else [],  # nothing spent before the agent ran
            }
        )  # fmt: skip
    else:
        parsed = parse(agent, stdout.decode(errors="replace"))
        usage, cost_lines = relay.report()
        done = process.returncode == 0 and parsed.finished
        error = None if done else parsed.error or stderr.decode(errors="replace").strip()[-2000:] or "agent failed"
        emit(
            {
                "event": "result",
                "status": "done" if done else "error",
                "answer": parsed.answer,
                "error": error,
                "step_log": parsed.steps,
                "usage": usage,
                "cost_lines": cost_lines,
            }
        )
    await asyncio.to_thread(sys.stdin.readline)
    # After the harness has read the final page: this ends the CLI's own background process.
    await detach()
    emit({"event": "stopped", "stopped": True})


def _argv(value: object) -> list[str]:
    return [str(part) for part in _items(value)]


async def _cli(command: list[str], env: dict[str, str], cwd: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, cwd=cwd,
    )  # fmt: skip
    _, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"{command[0]} {command[1]} failed: {err.decode(errors='replace').strip()[-500:]}")


def emit(message: dict[str, object]) -> None:
    print(json.dumps(message, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(run(json.loads(sys.stdin.readline())))
