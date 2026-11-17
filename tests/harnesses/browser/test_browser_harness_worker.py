from __future__ import annotations

import asyncio
import gzip
from pathlib import Path

import httpx
import pytest
import uvicorn
from parallax.harnesses.browser.browser_harness_worker import (
    AGENT_COMPONENT,
    Agent,
    Relay,
    Wire,
    agent_env,
    argv,
    billed,
    install_skill,
    parse_claude,
    parse_codex,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

FIXTURES = Path(__file__).with_name("fixtures")


def test_a_responses_stream_is_billed_what_openrouter_reports() -> None:
    bill = billed((FIXTURES / "openrouter-responses.sse").read_bytes(), "openai/gpt-6-luna", Wire.RESPONSES)
    assert (bill.cost_usd, bill.input, bill.output) == (3.8e-06, 8, 6)


def test_a_messages_stream_counts_cached_input_and_is_billed_what_openrouter_reports() -> None:
    bill = billed((FIXTURES / "openrouter-messages.sse").read_bytes(), "anthropic/claude-haiku-5.5", Wire.MESSAGES)
    assert bill.cost_usd is not None and bill.cost_usd > 0
    assert (bill.input, bill.output) == (11, 14)


def test_a_generation_without_a_reported_cost_is_unpriced() -> None:
    bill = billed(b'{"usage":{"input_tokens":5,"output_tokens":1}}', "m", Wire.RESPONSES)
    assert bill.cost_usd is None
    relay = Relay(bills=[bill])
    assert relay.report()[1] == [{"component": AGENT_COMPONENT, "dollars": None}]


def test_codex_events_give_the_answer_and_each_shell_command() -> None:
    run = parse_codex((FIXTURES / "codex-exec.jsonl").read_text())
    assert (run.answer, run.finished) == ("done", True)
    assert [(step["name"], step["output"].strip(), step["ok"]) for step in run.steps] == [
        ("shell", "parallax-fixture", True)
    ]


def test_claude_code_events_give_the_answer_and_each_tool_call_with_its_result() -> None:
    run = parse_claude((FIXTURES / "claude-code-stream.jsonl").read_text())
    assert (run.answer, run.finished) == ("done", True)
    assert [(step["name"], step["args"]["command"], step["output"]) for step in run.steps] == [
        ("Bash", "echo parallax-fixture", "parallax-fixture")
    ]


@pytest.mark.parametrize("compressed", [False, True], ids=["plain", "gzip"])
@pytest.mark.parametrize(
    ("path", "fixture", "wire"),
    [
        ("/api/v1/responses", "openrouter-responses.sse", Wire.RESPONSES),
        ("/api/v1/messages", "openrouter-messages.sse", Wire.MESSAGES),
    ],
)
async def test_the_relay_streams_a_generation_through_unchanged_and_keeps_its_bill(
    path: str, fixture: str, wire: Wire, compressed: bool
) -> None:
    recorded = (FIXTURES / fixture).read_bytes()
    seen: list[str | None] = []

    async def upstream(request: Request) -> Response:
        seen.append(request.headers.get("authorization"))
        if compressed:
            return Response(
                gzip.compress(recorded), media_type="text/event-stream", headers={"content-encoding": "gzip"}
            )
        return Response(recorded, media_type="text/event-stream")

    app = Starlette(routes=[Route(path, upstream, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    relay = Relay(upstream=f"http://127.0.0.1:{port}")
    try:
        async with relay.serving() as url, httpx.AsyncClient() as http:
            response = await http.post(
                f"{url}{path}", json={"model": "m", "stream": True}, headers={"authorization": "Bearer k"}
            )
            # The relay records a bill as its stream closes, just after the client has the last byte.
            for _ in range(100):
                if relay.bills:
                    break
                await asyncio.sleep(0.01)
    finally:
        server.should_exit = True
        await task
    assert response.content == recorded
    assert seen == ["Bearer k"]
    assert relay.bills == [billed(recorded, "m", wire)]


@pytest.mark.parametrize(
    ("agent", "directory"),
    [(Agent.CODEX, ".agents/skills/agent-browser"), (Agent.CLAUDE_CODE, ".claude/skills/agent-browser")],
)
def test_a_skill_is_installed_under_its_own_name_where_the_agent_reads_skills(
    tmp_path: Path, agent: Agent, directory: str
) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("skill")
    install_skill(agent, tmp_path, skill, "agent-browser")
    assert (tmp_path / directory / "SKILL.md").read_text() == "skill"


def test_codex_runs_in_its_own_home_and_passes_its_environment_to_the_shell(tmp_path: Path) -> None:
    env = agent_env(
        Agent.CODEX, "m", "http://relay", {"OPENROUTER_API_KEY": "k", "AGENT_BROWSER_CDP": "ws://x"}, tmp_path
    )
    assert env["CODEX_HOME"] == str(tmp_path) and env["AGENT_BROWSER_CDP"] == "ws://x"
    assert 'shell_environment_policy.inherit="all"' in argv(Agent.CODEX, "codex", "m", "goal", "http://relay")


async def test_the_cli_is_detached_once_even_when_the_trial_is_interrupted(tmp_path: Path) -> None:
    from parallax.harnesses.browser.browser_harness_worker import Detach

    marks = tmp_path / "marks"
    detach = Detach(["sh", "-c", f"echo x >> {marks}"], {}, tmp_path)
    await detach()
    await detach()
    assert marks.read_text() == "x\n"
