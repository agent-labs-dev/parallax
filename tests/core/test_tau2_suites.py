import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from parallax import official_suites as official
from parallax import tau2_bridge
from parallax.extensions import GradeRequest
from parallax.judge import JudgeConfig, JudgePanel
from parallax.models import Area, Capability, Provenance, TaskRecord
from parallax.task import task_from_record

TASKS = {
    "airline": [{"id": "0", "user_scenario": {"instructions": {"reason_for_call": "Cancel my flight"}}}],
    "retail": [{"id": "7", "user_scenario": {"instructions": {"reason_for_call": "Exchange my order"}}}],
    "telecom": [{"id": "t-1", "user_scenario": {"instructions": {"reason_for_call": "Fix my data"}}}],
}
SPLITS = {"airline": {"base": ["0"]}, "retail": {"base": ["7"]}, "telecom": {"base": ["t-1"]}}

TRANSCRIPT = [
    {"role": "user", "content": "Create a new task called 'Important Meeting' for user_1."},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "name": "create_task",
                "arguments": {"user_id": "user_1", "title": "Important Meeting"},
                "requestor": "assistant",
            }
        ],
    },
    {
        "role": "tool",
        "id": "c1",
        "content": '{"task_id": "task_2", "title": "Important Meeting", "description": null, "status": "pending"}',
        "requestor": "assistant",
        "error": False,
    },
    {"role": "assistant", "content": "Done, the task was created."},
]


def _files(domain: str) -> dict[str, bytes]:
    policy = f"# {domain} policy\nBe helpful."
    return {
        "tasks.json": json.dumps(TASKS[domain]).encode(),
        "split_tasks.json": json.dumps(SPLITS[domain]).encode(),
        official.TAU2_POLICY_FILES[domain]: policy.encode(),
    }


def test_tau2_records_carry_only_frozen_data_keys(monkeypatch):
    monkeypatch.setattr(official, "_fetch_tau2_file", lambda domain, name: _files(domain)[name])
    monkeypatch.setattr(official, "TAU2_EXPECTED_BASE", {"airline": 1, "retail": 1, "telecom": 1})
    records = list(official.tau2_tasks())
    assert [r.id for r in records] == ["tau2-airline-0", "tau2-retail-7", "tau2-telecom-t-1"]
    for record in records:
        assert record.grading == "tau2-upstream-v1"
        assert set(record.data) == {"domain", "upstream_task_id", "policy_ref", "task", "task_digest"}
        assert record.data["task_digest"] == official.tau2_task_digest(record.data["task"])
        assert official.tau2_task_from_data(record.data) == record.data["task"]
        assert record.data["policy_ref"].endswith(f"@{official.TAU2_UPSTREAM_COMMIT}")
    assert "Be helpful." in records[0].prompt
    assert "Cancel my flight" not in records[0].prompt  # the scenario never enters the agent prompt


def test_tau2_world_server_uses_the_shared_public_url_contract():
    from parallax.runner import advertised_world_mcp_url, world_bind_host

    assert world_bind_host(None) == "127.0.0.1"
    assert world_bind_host("https://world.example") == "0.0.0.0"
    assert advertised_world_mcp_url(None, "http://127.0.0.1:1234") == "http://127.0.0.1:1234/mcp"
    assert advertised_world_mcp_url("https://world.example/", "http://127.0.0.1:1234") == "https://world.example/mcp"


def _tau2_request(recorded, *, domain: str = "airline", task_id: str = "0"):
    full_task = {"id": task_id, "user_scenario": {"instructions": "scenario"}}
    record = TaskRecord(
        id="tau2-airline-0",
        version=1,
        area=Area.SAAS,
        summary="tau2 task",
        prompt="policy",
        grading=official.TAU2_POLICY,
        requires=frozenset({Capability.MCP, Capability.MULTI_TURN}),
        maturity="regression",
        provenance=Provenance.CONVERTED,
        data={
            "domain": domain,
            "upstream_task_id": task_id,
            "policy_ref": "policy.md@pin",
            "task": full_task,
            "task_digest": official.tau2_task_digest(full_task),
        },
    )
    task = task_from_record(official.TAU2, record)
    return GradeRequest(
        task=task,
        evidence=None,
        answers=("done",),
        product=None,
        execution={},
        harness_model=None,
        panel=JudgePanel(models=("configured/model",)),
        judge_config=JudgeConfig(base_url="https://judge.invalid/v1", api_key="test"),
        client=httpx.AsyncClient(),
        runner_recorded=recorded,
    )


# --- pure evidence validation (no checkout needed) -------------------------


def test_tau2_evidence_requires_termination_reason_with_no_default():
    with pytest.raises(ValueError, match="termination_reason"):
        official.parse_tau2_evidence({"transcript": TRANSCRIPT})
    evidence = official.parse_tau2_evidence({"transcript": TRANSCRIPT, "termination_reason": "agent_stop"})
    assert evidence.termination_reason == "agent_stop"


def test_tau2_evidence_rejects_unknown_termination_reason():
    with pytest.raises(ValueError):
        official.parse_tau2_evidence({"transcript": TRANSCRIPT, "termination_reason": "finished"})


def test_tau2_evidence_rejects_tool_call_without_following_tool_message():
    transcript = [TRANSCRIPT[0], TRANSCRIPT[1], {"role": "assistant", "content": "done"}]
    with pytest.raises(ValueError, match="ToolMessage"):
        official.parse_tau2_evidence({"transcript": transcript, "termination_reason": "agent_stop"})


def test_tau2_evidence_rejects_string_tool_arguments():
    transcript = json.loads(json.dumps(TRANSCRIPT))
    transcript[1]["tool_calls"][0]["arguments"] = '{"user_id": "user_1"}'
    with pytest.raises(ValueError):
        official.parse_tau2_evidence({"transcript": transcript, "termination_reason": "agent_stop"})


def test_tau2_evidence_rejects_bare_tool_calls_and_final_db_state():
    with pytest.raises(ValueError):
        official.parse_tau2_evidence({"transcript": TRANSCRIPT, "termination_reason": "agent_stop", "tool_calls": []})
    with pytest.raises(ValueError):
        official.parse_tau2_evidence(
            {"transcript": TRANSCRIPT, "termination_reason": "agent_stop", "final_db_state": {}}
        )


def test_tau2_bridge_messages_reject_the_same_contract_breaks():
    with pytest.raises(RuntimeError, match="termination_reason"):
        tau2_bridge._messages({"transcript": TRANSCRIPT})
    with pytest.raises(RuntimeError, match="bare tool_calls"):
        tau2_bridge._messages(
            {"transcript": TRANSCRIPT, "termination_reason": "agent_stop", "tool_calls": [{"name": "x"}]}
        )
    messages = tau2_bridge._messages({"transcript": TRANSCRIPT, "termination_reason": "max_steps"})
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]


def test_tau2_judge_calls_become_a_priced_judgment():
    request = _tau2_request(None)
    payload = {"domain": "mock", "task_id": "x"}
    result = {
        "passed": True,
        "score": 1.0,
        "reason": "ok",
        "calls": [
            {"model": "gpt-5.5", "input_tokens": 10, "output_tokens": 3, "cost_usd": 0.01, "latency_ms": 5},
            {
                "model": "gpt-5.4-mini-2026-03-17",
                "input_tokens": 7,
                "output_tokens": 2,
                "cost_usd": None,
                "latency_ms": 4,
            },
        ],
    }
    judgment = official._tau2_judgment(request, payload, result)
    assert judgment is not None
    assert [call.cost_usd for call in judgment.fallback_calls] == [0.01, None]
    assert judgment.cost() == (0.01, 1)  # priced where known, unpriced where not, never dropped


@pytest.mark.asyncio
async def test_tau2_grade_without_valid_evidence_is_unjudged():
    request = _tau2_request({"transcript": TRANSCRIPT})
    graded = await official._grade_tau2(request)
    await request.client.aclose()
    assert graded.checks[0].passed is None
    assert "termination_reason" in graded.checks[0].detail


def test_tau2_judge_env_is_an_allowlist(monkeypatch):
    monkeypatch.setenv("PARALLAX_JUDGE_BASE_URL", "https://gateway.invalid/v1")
    monkeypatch.setenv("PARALLAX_JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("OPENAI_API_KEY", "simulator-key-must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "simulator-key-must-not-leak")
    env = official._tau2_judge_env(_tau2_request(None))
    assert env == {"OPENAI_BASE_URL": "https://judge.invalid/v1", "OPENAI_API_KEY": "test"}


def test_tau2_termination_reasons_have_one_source():
    from parallax.tau2_terms import TERMINATION_REASONS

    assert TERMINATION_REASONS == tau2_bridge.TERMINATION_REASONS
    assert "agent_stop" in TERMINATION_REASONS


def test_tau2_progress_matches_upstream_message_hops_and_precedence():
    from parallax.tau2_terms import Tau2Event, tau2_progress

    sequential = (
        [Tau2Event("user")]
        + [event for _ in range(3) for event in (Tau2Event("agent", waits_for_environment=True), Tau2Event("env"))]
        + [Tau2Event("agent")]
    )
    assert tau2_progress(sequential, max_steps=20, max_errors=2).step_count == 8

    parallel = [Tau2Event("user"), Tau2Event("agent", waits_for_environment=True), Tau2Event("env"), Tau2Event("agent")]
    assert tau2_progress(parallel, max_steps=20, max_errors=2).step_count == 4

    user_tools = [
        Tau2Event("user", waits_for_environment=True),
        Tau2Event("env"),
        Tau2Event("user", waits_for_environment=True),
        Tau2Event("env"),
        Tau2Event("user"),
    ]
    assert tau2_progress(user_tools, max_steps=20, max_errors=2).step_count == 5

    stopped = tau2_progress([Tau2Event("user", stop=True)], max_steps=5, max_errors=1)
    assert stopped.termination == "user_stop"
    overridden = tau2_progress(
        [Tau2Event("user", waits_for_environment=True, stop=True), Tau2Event("env", error=True)],
        max_steps=1,
        max_errors=1,
    )
    assert overridden.termination == "too_many_errors"


def test_tau2_read_judge_calls_reads_only_the_side_file(tmp_path):
    calls_file = tmp_path / "calls.jsonl"
    calls_file.write_text('{"model": "gpt-5.5", "cost_usd": null, "latency_ms": 3}\nnot json\n')
    assert official._read_judge_calls(calls_file) == [{"model": "gpt-5.5", "cost_usd": None, "latency_ms": 3}]
    assert official._read_judge_calls(tmp_path / "missing.jsonl") == []


def test_tau2_bridge_error_reduces_stderr_to_class_and_fixed_reason():
    assert (
        official._bridge_error("noise\nRuntimeError: judge down with a secret\n") == "RuntimeError: tau2 bridge failed"
    )
    assert official._bridge_error("") == "Error: tau2 bridge failed"


def test_tau2_failed_call_error_label_is_class_and_status_only():
    class GatewayDown(Exception):
        status_code = 503

    assert tau2_bridge._error_label(GatewayDown("secret gateway message")) == "GatewayDown (HTTP 503)"
    assert tau2_bridge._error_label(RuntimeError("secret message")) == "RuntimeError"
    assert "secret" not in tau2_bridge.failure(RuntimeError("secret message"))


@pytest.mark.asyncio
async def test_tau2_preflight_queries_gateway_once_and_refuses_missing_judge(monkeypatch, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").touch()
    monkeypatch.setenv("PARALLAX_OFFICIAL_TAU2_ROOT", str(tmp_path))
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json.dumps({"data": [{"id": official.TAU2_COMMUNICATE_JUDGE_MODEL}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        import dataclasses

        request = dataclasses.replace(
            _tau2_request(None),
            judge_config=JudgeConfig(base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="test"),
        )
        first = await official._tau2_preflight(request)
        second = await official._tau2_preflight(request)
    finally:
        server.shutdown()
    assert first is not None and official.TAU2_NL_JUDGE_MODEL in first
    assert second == first
    assert seen == ["/v1/models", "/v1/models"]  # failures are not cached: the gateway is probed again


# --- real pinned checkout, `mock` domain (skipped where unavailable) ------


def _tau2_checkout() -> tuple[Path, Path] | None:
    root_text = os.environ.get("PARALLAX_OFFICIAL_TAU2_ROOT")
    candidates = [Path(root_text)] if root_text else []
    candidates.append(Path.home() / ".cache" / "agent-scratch" / "harness-scoreboard" / "upstream" / "tau2")
    for root in candidates:
        python = root / ".venv" / "bin" / "python"
        if root.is_dir() and python.is_file():
            return root, python
    return None


requires_tau2 = pytest.mark.skipif(
    _tau2_checkout() is None,
    reason="no pinned tau2 checkout with a .venv (set PARALLAX_OFFICIAL_TAU2_ROOT)",
)


@requires_tau2
@pytest.mark.asyncio
async def test_tau2_bridge_agent_stop_passes_and_max_steps_scores_zero(monkeypatch):
    # The preflight needs a gateway offering both judge ids; this task's reward basis calls
    # neither judge, so the gateway only ever serves its model list.
    import dataclasses
    from http.server import BaseHTTPRequestHandler

    class Models(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(
                {"data": [{"id": official.TAU2_COMMUNICATE_JUDGE_MODEL}, {"id": official.TAU2_NL_JUDGE_MODEL}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    base_url, server = _gateway(Models)
    root, _python = _tau2_checkout()  # type: ignore[misc]
    monkeypatch.setenv("PARALLAX_OFFICIAL_TAU2_ROOT", str(root))
    config = JudgeConfig(base_url=base_url, api_key="test")
    try:
        product = {"transcript": TRANSCRIPT, "termination_reason": "agent_stop"}
        request = dataclasses.replace(
            _tau2_request(product, domain="mock", task_id="create_task_1_with_env_assertions"), judge_config=config
        )
        graded = await official._grade_tau2(request)
        await request.client.aclose()
        assert graded.checks[0].passed is True
        assert graded.score == 1.0
        # No communicate or NL judge was in this task's reward basis, so no judge calls are priced.
        assert graded.judgments == ()

        product = {"transcript": TRANSCRIPT, "termination_reason": "max_steps"}
        request = dataclasses.replace(
            _tau2_request(product, domain="mock", task_id="create_task_1_with_env_assertions"), judge_config=config
        )
        graded = await official._grade_tau2(request)
        await request.client.aclose()
        assert graded.checks[0].passed is False
        assert graded.score == 0.0
    finally:
        server.shutdown()  # type: ignore[attr-defined]


@requires_tau2
def test_tau2_bridge_script_against_mock_domain():
    root, python = _tau2_checkout()  # type: ignore[misc]
    payload = {
        "operation": "evaluate",
        "suite": "tau2",
        "root": str(root),
        "commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip(),
        "domain": "mock",
        "task_id": "create_task_1_with_env_assertions",
        "transcript": TRANSCRIPT,
        "termination_reason": "agent_stop",
    }
    # The bridge pins the upstream commit, which for a local checkout is its HEAD; the
    # parallax-side pin is exercised through _grade_tau2 above with the real checkout.
    if payload["commit"] != official.TAU2_UPSTREAM_COMMIT:
        pytest.skip(f"tau2 checkout at {payload['commit'][:8]}, not the pinned {official.TAU2_UPSTREAM_COMMIT[:8]}")
    result = subprocess.run(
        [str(python), "-B", str(Path(official.__file__).with_name("tau2_bridge.py"))],
        input=json.dumps(payload).encode(),
        capture_output=True,
        cwd=root,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()[-500:]
    outcome = json.loads(result.stdout)
    assert outcome["passed"] is True
    assert "calls" not in outcome  # judge calls travel only in the side file, never on stdout


def _gateway(handler) -> tuple[str, object]:
    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/v1", server


def _nl_payload() -> dict:
    # `update_task_with_initialization_data` carries communicate_info, so the semantic
    # communicate judge (in the default reward basis) is called on the assistant text.
    transcript = [
        {"role": "user", "content": "Continue our earlier discussion and mark task_2 as completed."},
        {
            "role": "assistant",
            "content": "As we discussed earlier about task_2, I have marked it as completed successfully.",
        },
    ]
    return {
        "operation": "evaluate",
        "suite": "tau2",
        "commit": official.TAU2_UPSTREAM_COMMIT,
        "domain": "mock",
        "task_id": "update_task_with_initialization_data",
        "transcript": transcript,
        "termination_reason": "agent_stop",
        "judge_models": {"communicate": official.TAU2_COMMUNICATE_JUDGE_MODEL, "nl": official.TAU2_NL_JUDGE_MODEL},
    }


@requires_tau2
@pytest.mark.asyncio
async def test_tau2_bridge_timeout_kills_the_process_group(monkeypatch):
    # The real bridge subprocess against the pinned checkout, with a gateway that never
    # answers: a tiny bridge timeout must kill the whole process group and return promptly.
    import time
    from http.server import BaseHTTPRequestHandler

    class Hanging(BaseHTTPRequestHandler):
        def do_POST(self):
            time.sleep(60)

        def log_message(self, *args):
            pass

    base_url, server = _gateway(Hanging)
    root, _python = _tau2_checkout()  # type: ignore[misc]
    monkeypatch.setenv("PARALLAX_OFFICIAL_TAU2_ROOT", str(root))
    monkeypatch.setattr(official, "TAU2_BRIDGE_TIMEOUT_S", 3.0)
    import dataclasses

    request = dataclasses.replace(_tau2_request(None), judge_config=JudgeConfig(base_url=base_url, api_key="test"))
    started = time.monotonic()
    try:
        result = await official._tau2_bridge(request, _nl_payload())
    finally:
        server.shutdown()  # type: ignore[attr-defined]
    assert time.monotonic() - started < 30
    assert "timed out" in result["error"]
    assert result["calls"] == []


@requires_tau2
@pytest.mark.asyncio
async def test_tau2_grade_bridge_failure_records_calls_and_no_unpriced_stand_in(monkeypatch):
    # Through _grade_tau2 with the real bridge: the gateway offers both judge ids (preflight
    # passes) then refuses every judge call. The recorded failed call prices as unpriced, the
    # grade detail carries no raw gateway text, and no stand-in unpriced call is added.
    from http.server import BaseHTTPRequestHandler

    marker = "gateway-secret-marker"

    class Gateway(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(
                {"data": [{"id": official.TAU2_COMMUNICATE_JUDGE_MODEL}, {"id": official.TAU2_NL_JUDGE_MODEL}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.dumps({"error": {"message": marker, "type": "invalid_request_error"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    base_url, server = _gateway(Gateway)
    root, _python = _tau2_checkout()  # type: ignore[misc]
    monkeypatch.setenv("PARALLAX_OFFICIAL_TAU2_ROOT", str(root))
    import dataclasses

    product = {"transcript": _nl_payload()["transcript"], "termination_reason": "agent_stop"}
    request = dataclasses.replace(
        _tau2_request(product, domain="mock", task_id="update_task_with_initialization_data"),
        judge_config=JudgeConfig(base_url=base_url, api_key="test"),
    )
    try:
        graded = await official._grade_tau2(request)
    finally:
        server.shutdown()  # type: ignore[attr-defined]
        await request.client.aclose()
    assert graded.checks[0].passed is None
    assert marker not in graded.checks[0].detail
    assert graded.unpriced_calls == 0
    if graded.judgments:
        assert graded.judgments[0].fallback_calls[0].cost_usd is None
        assert graded.judgments[0].cost() == (0.0, 1)


@requires_tau2
@pytest.mark.asyncio
async def test_tau2_bridge_failed_judge_call_is_captured_in_the_side_file_only(monkeypatch):
    # The real bridge subprocess: the gateway refuses the judge call with a marker message.
    # The side file records the call with class and status only; neither the recorded error
    # nor the grade-facing bridge error may carry the gateway's raw message.
    from http.server import BaseHTTPRequestHandler

    marker = "gateway-secret-marker"

    class Refusing(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps({"error": {"message": marker, "type": "invalid_request_error"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    base_url, server = _gateway(Refusing)
    root, _python = _tau2_checkout()  # type: ignore[misc]
    monkeypatch.setenv("PARALLAX_OFFICIAL_TAU2_ROOT", str(root))
    import dataclasses

    request = dataclasses.replace(_tau2_request(None), judge_config=JudgeConfig(base_url=base_url, api_key="test"))
    try:
        result = await official._tau2_bridge(request, _nl_payload())
    finally:
        server.shutdown()  # type: ignore[attr-defined]
    assert marker not in result["error"]
    assert result["error"].endswith("tau2 bridge failed")
    assert result["calls"], "the failed judge call was not recorded in the side file"
    recorded = result["calls"][0]
    assert recorded["cost_usd"] is None
    assert marker not in str(recorded.get("error"))
    assert "400" in str(recorded.get("error")) or recorded.get("error")


# --- parallax-owned loop (scripted harness, fake runtime; no docker/network) ---

# Upstream exposes no scripted user simulator (DummyUser raises), so the loop test stands in
# a scripted runtime at parallax's own seam; the real-checkout path stays skip-gated below.


class _FakeTau2Runtime:
    tools = [{"name": "create_task", "description": "create", "parameters": {"type": "object", "properties": {}}}]
    simulator_cost_usd = 0.25
    max_steps = 200
    max_errors = 10
    tool_errors = 0
    step_count = 0

    async def start(self, *, domain, task):
        self.step_count = 0
        self.tool_errors = 0
        return self.tools

    async def call_tool(self, name, arguments, call_id):
        self.step_count += 1
        return {"content": '{"task_id": "task_2", "status": "pending"}', "error": False, "id": call_id}

    def agent_step(self):
        self.step_count += 1

    async def user_turn(self, *, first, assistant="", remaining_steps=None):
        self.step_count += 1
        if first:
            return {"messages": [{"role": "user", "content": "Create a task called Important Meeting."}], "stop": False}
        return {"messages": [{"role": "user", "content": "Thanks. ###STOP###"}], "stop": True}

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_tau2_grade_never_uses_harness_reported_evidence():
    import dataclasses

    forged = dataclasses.replace(
        _tau2_request(None), product={"tau2": {"transcript": TRANSCRIPT, "termination_reason": "agent_stop"}}
    )
    graded = await official._grade_tau2(forged)
    await forged.client.aclose()
    assert graded.checks[0].passed is None
    assert "parallax recorded no tau2 transcript" in graded.checks[0].detail


@requires_tau2
@pytest.mark.asyncio
async def test_tau2_serve_runtime_on_mock_domain_with_scripted_gateway():
    # Real pinned checkout, `mock` domain: a scripted OpenAI-compatible gateway stands in for
    # the simulator's model (upstream exposes no scripted simulator), so no paid call is made.
    from http.server import BaseHTTPRequestHandler

    from parallax.tau2_runtime import Tau2BridgeRuntime

    calls = {"n": 0, "temperatures": []}

    class Gateway(BaseHTTPRequestHandler):
        def do_POST(self):
            calls["n"] += 1
            calls["temperatures"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            text = "Create a new task called 'Important Meeting' for user_1." if calls["n"] == 1 else "###STOP###"
            body = json.dumps(
                {
                    "id": "x",
                    "object": "chat.completion",
                    "model": official.TAU2_SIMULATOR_MODEL,
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    base_url, server = _gateway(Gateway)
    root, python = _tau2_checkout()  # type: ignore[misc]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    if head != official.TAU2_UPSTREAM_COMMIT:
        server.shutdown()  # type: ignore[attr-defined]
        pytest.skip(f"tau2 checkout at {head[:8]}, not the pinned {official.TAU2_UPSTREAM_COMMIT[:8]}")
    runtime = Tau2BridgeRuntime(
        root=root, python=python, commit=official.TAU2_UPSTREAM_COMMIT, base_url=base_url, api_key="test"
    )
    mock_tasks = json.loads((root / "data/tau2/domains/mock/tasks.json").read_text())
    task = next(t for t in mock_tasks if t["id"] == "create_task_1")
    try:
        tools = await runtime.start(domain="mock", task=task)
        assert any(t["name"] == "create_task" for t in tools)
        defaults = json.loads(
            subprocess.run(
                [
                    str(python),
                    "-c",
                    'import json; from tau2 import config; print(json.dumps({"steps": config.DEFAULT_MAX_STEPS, "errors": config.DEFAULT_MAX_ERRORS, "temperature": config.DEFAULT_LLM_ARGS_USER["temperature"]}))',
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        assert runtime.max_steps == defaults["steps"]
        assert runtime.max_errors == defaults["errors"]
        first = await runtime.user_turn(first=True)
        assert "Important Meeting" in str(first["messages"])
        assert calls["temperatures"][0]["temperature"] == defaults["temperature"] == 0.0
        result = await runtime.call_tool("create_task", {"user_id": "user_1", "title": "Important Meeting"}, "c1")
        assert not result["error"]
        second = await runtime.user_turn(first=False, assistant="Done, the task was created.")
        assert second["stop"] is True
    finally:
        await runtime.close()
        server.shutdown()  # type: ignore[attr-defined]


@requires_tau2
@pytest.mark.asyncio
async def test_tau2_serve_uses_upstream_string_for_structured_scenario(monkeypatch):
    """A real pinned task with a structured scenario must boot the upstream user simulator."""
    from http.server import BaseHTTPRequestHandler

    requests: list[dict] = []

    class Gateway(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(
                {
                    "id": "x",
                    "object": "chat.completion",
                    "model": official.TAU2_SIMULATOR_MODEL,
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "###STOP###"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    base_url, server = _gateway(Gateway)
    root, python = _tau2_checkout()  # type: ignore[misc]
    task = json.loads((root / "data/tau2/domains/airline/tasks.json").read_text())[0]
    runtime = __import__("parallax.tau2_runtime", fromlist=["Tau2BridgeRuntime"]).Tau2BridgeRuntime(
        root=root, python=python, commit=official.TAU2_UPSTREAM_COMMIT, base_url=base_url, api_key="test"
    )
    try:
        await runtime.start(domain="airline", task=task)
        await runtime.user_turn(first=True)
    finally:
        await runtime.close()
        server.shutdown()  # type: ignore[attr-defined]
    assert requests
    contents = [str(message.get("content")) for message in requests[0]["messages"]]
    assert any("Reason for call:" in content for content in contents)


@pytest.mark.asyncio
async def test_tau2_runner_loop_records_transcript_and_simulator_cost(tmp_path, monkeypatch):
    import parallax.runner as runner
    from parallax.harnesses.oracle import ORACLE_INFO
    from parallax.models import Isolation, TurnOutput, TurnStatus
    from parallax.world.client import WorldClient

    full_task = {"id": "create_task_1", "user_scenario": {"instructions": "Create a task called Important Meeting"}}
    record = TaskRecord(
        id="tau2-mock-1", version=1, area=Area.SAAS, summary="tau2 mock", prompt="Mock policy. Be helpful.",
        grading=official.TAU2_POLICY, requires=frozenset({Capability.MCP, Capability.MULTI_TURN}),
        maturity="regression", provenance=Provenance.CONVERTED,
        data={"domain": "mock", "upstream_task_id": "create_task_1", "policy_ref": "policy.md@pin",
              "task": full_task, "task_digest": official.tau2_task_digest(full_task)},
    )  # fmt: skip
    task = task_from_record(official.TAU2, record)

    class ScriptedHarness:
        info = ORACLE_INFO.model_copy(update={"name": "scripted-tau2"})

        async def start(self, trial, box):
            self._trial = trial

        async def turn(self, t):
            async with WorldClient(self._trial.mcp.url, self._trial.env["PARALLAX_WORLD_TOKEN"]) as client:
                ok, _value = await client.call("create_task", {"user_id": "user_1", "title": "Important Meeting"})
            assert ok
            return TurnOutput(status=TurnStatus.COMPLETED, answer="Created the task.", session_id=None)

        async def stop(self):
            return None

        async def version(self, box):
            return "scripted-1"

    monkeypatch.setattr(runner, "load_harness", lambda name: ScriptedHarness())
    monkeypatch.setattr(runner.TrialExecution, "_make_tau2_runtime", lambda self: _FakeTau2Runtime())
    out = tmp_path / "run-tau2"
    await runner.run_async(
        [task],
        ["scripted-tau2"],
        [None],
        runner.RunOptions(out=out, isolation=Isolation.HOST, quiet=True, retry_invalid=0),
    )
    trajectories = list(out.glob("trials/**/trajectory.json"))
    assert trajectories, "no trial trajectory written"
    trajectory = json.loads(trajectories[0].read_text())
    recorded = trajectory["extra"]["tau2_recorded"]
    assert recorded["termination_reason"] == "user_stop"
    assert [m["role"] for m in recorded["transcript"]] == ["user", "assistant", "tool", "assistant", "user"]
    assert recorded["transcript"][1]["tool_calls"][0]["name"] == "create_task"
    (row,) = runner.read_results(out / "results.jsonl")
    assert row.metrics["simulator_cost_usd"] == 0.25  # simulator cost, out of harness usage
    assert all(u.cost_usd != 0.25 for u in row.usage)


@pytest.mark.asyncio
async def test_tau2_runner_max_steps_counts_agent_and_tool_hops(tmp_path, monkeypatch):
    import parallax.runner as runner
    from parallax.harnesses.oracle import ORACLE_INFO
    from parallax.models import Isolation, TurnOutput, TurnStatus
    from parallax.world.client import WorldClient

    class ShortRuntime(_FakeTau2Runtime):
        max_steps = 3

    full_task = {"id": "create_task_1", "user_scenario": {"instructions": "Create a task"}}
    record = TaskRecord(
        id="tau2-mock-short", version=1, area=Area.SAAS, summary="tau2 mock", prompt="Mock policy.",
        grading=official.TAU2_POLICY, requires=frozenset({Capability.MCP, Capability.MULTI_TURN}),
        maturity="regression", provenance=Provenance.CONVERTED,
        data={"domain": "mock", "upstream_task_id": "create_task_1", "policy_ref": "policy.md@pin",
              "task": full_task, "task_digest": official.tau2_task_digest(full_task)},
    )  # fmt: skip
    task = task_from_record(official.TAU2, record)

    class ScriptedHarness:
        info = ORACLE_INFO.model_copy(update={"name": "scripted-tau2-short"})

        async def start(self, trial, box):
            self._trial = trial

        async def turn(self, t):
            async with WorldClient(self._trial.mcp.url, self._trial.env["PARALLAX_WORLD_TOKEN"]) as client:
                await client.call("create_task", {"user_id": "user_1", "title": "Important Meeting"})
            return TurnOutput(status=TurnStatus.COMPLETED, answer="Created the task.", session_id=None)

        async def stop(self):
            return None

        async def version(self, box):
            return "scripted-1"

    monkeypatch.setattr(runner, "load_harness", lambda name: ScriptedHarness())
    monkeypatch.setattr(runner.TrialExecution, "_make_tau2_runtime", lambda self: ShortRuntime())
    out = tmp_path / "run-tau2-short"
    await runner.run_async(
        [task],
        ["scripted-tau2-short"],
        [None],
        runner.RunOptions(out=out, isolation=Isolation.HOST, quiet=True, retry_invalid=0),
    )
    trajectory = json.loads(next(out.glob("trials/**/trajectory.json")).read_text())
    assert trajectory["extra"]["tau2_recorded"]["termination_reason"] == "max_steps"


@pytest.mark.parametrize(("delivered", "answer"), [(True, "###STOP###"), (False, "")])
@pytest.mark.asyncio
async def test_tau2_too_many_errors_overrides_agent_result(delivered, answer):
    import types

    import parallax.runner as runner
    from parallax.runner import Transcript
    from parallax.tau2_runtime import Tau2ToolLog

    class Runtime:
        max_steps = 200
        max_errors = 1
        simulator_cost_usd = None
        tool_errors = 0
        step_count = 0

        def agent_step(self):
            self.step_count += 1

        async def user_turn(self, *, first, assistant="", remaining_steps=None):
            return {"messages": [{"role": "user", "content": "Continue"}], "stop": False}

    runtime = Runtime()
    trial = cast(Any, object.__new__(runner.TrialExecution))
    trial.task = types.SimpleNamespace(turns=[types.SimpleNamespace(text="Policy")])
    trial.t = Transcript(answers=[""])
    trial._tau2_runtime = runtime
    trial._tau2_log = Tau2ToolLog(cast(Any, runtime))

    async def deliver(_harness, _index, _text, _new_session, _attached):
        trial._tau2_log.entries.append(
            {"id": "error", "name": "tool", "arguments": {}, "content": "error", "error": True, "batch": 1}
        )
        trial.t.answers[0] = answer
        return delivered

    trial.deliver = deliver
    await trial.tau2_loop(cast(Any, None))
    assert trial.tau2_recorded["termination_reason"] == "too_many_errors"
