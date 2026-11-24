import json
import os
import subprocess
from pathlib import Path

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
        assert set(record.data) == {"domain", "upstream_task_id", "policy_ref"}
        assert record.data["policy_ref"].endswith(f"@{official.TAU2_UPSTREAM_COMMIT}")
    assert "Be helpful." in records[0].prompt


def _tau2_request(product, *, domain: str = "airline", task_id: str = "0"):
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
        data={"domain": domain, "upstream_task_id": task_id, "policy_ref": "policy.md@pin"},
    )
    task = task_from_record(official.TAU2, record)
    return GradeRequest(
        task=task,
        evidence=None,
        answers=("done",),
        product=product,
        execution={},
        harness_model=None,
        panel=JudgePanel(models=("configured/model",)),
        judge_config=JudgeConfig(base_url="https://judge.invalid/v1", api_key="test"),
        client=httpx.AsyncClient(),
    )


# --- pure evidence validation (no checkout needed) -------------------------


def test_tau2_evidence_requires_termination_reason_with_no_default():
    with pytest.raises(ValueError, match="termination_reason"):
        official.parse_tau2_evidence({"tau2": {"transcript": TRANSCRIPT}})
    evidence = official.parse_tau2_evidence({"tau2": {"transcript": TRANSCRIPT, "termination_reason": "agent_stop"}})
    assert evidence.termination_reason == "agent_stop"


def test_tau2_evidence_rejects_unknown_termination_reason():
    with pytest.raises(ValueError):
        official.parse_tau2_evidence({"tau2": {"transcript": TRANSCRIPT, "termination_reason": "finished"}})


def test_tau2_evidence_rejects_tool_call_without_following_tool_message():
    transcript = [TRANSCRIPT[0], TRANSCRIPT[1], {"role": "assistant", "content": "done"}]
    with pytest.raises(ValueError, match="ToolMessage"):
        official.parse_tau2_evidence({"tau2": {"transcript": transcript, "termination_reason": "agent_stop"}})


def test_tau2_evidence_rejects_string_tool_arguments():
    transcript = json.loads(json.dumps(TRANSCRIPT))
    transcript[1]["tool_calls"][0]["arguments"] = '{"user_id": "user_1"}'
    with pytest.raises(ValueError):
        official.parse_tau2_evidence({"tau2": {"transcript": transcript, "termination_reason": "agent_stop"}})


def test_tau2_evidence_rejects_bare_tool_calls_and_final_db_state():
    with pytest.raises(ValueError):
        official.parse_tau2_evidence(
            {"tau2": {"transcript": TRANSCRIPT, "termination_reason": "agent_stop", "tool_calls": []}}
        )
    with pytest.raises(ValueError):
        official.parse_tau2_evidence(
            {"tau2": {"transcript": TRANSCRIPT, "termination_reason": "agent_stop", "final_db_state": {}}}
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
    request = _tau2_request({"tau2": {"transcript": TRANSCRIPT}})
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
    assert seen == ["/v1/models"]  # cached per run: the gateway is queried once


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
        product = {"tau2": {"transcript": TRANSCRIPT, "termination_reason": "agent_stop"}}
        request = dataclasses.replace(
            _tau2_request(product, domain="mock", task_id="create_task_1_with_env_assertions"), judge_config=config
        )
        graded = await official._grade_tau2(request)
        await request.client.aclose()
        assert graded.checks[0].passed is True
        assert graded.score == 1.0
        # No communicate or NL judge was in this task's reward basis, so no judge calls are priced.
        assert graded.judgments == ()

        product = {"tau2": {"transcript": TRANSCRIPT, "termination_reason": "max_steps"}}
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

    product = {"tau2": {"transcript": _nl_payload()["transcript"], "termination_reason": "agent_stop"}}
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
