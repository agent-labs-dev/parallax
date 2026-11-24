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
    root, _python = _tau2_checkout()  # type: ignore[misc]
    monkeypatch.setenv("PARALLAX_OFFICIAL_TAU2_ROOT", str(root))
    product = {"tau2": {"transcript": TRANSCRIPT, "termination_reason": "agent_stop"}}
    request = _tau2_request(product, domain="mock", task_id="create_task_1_with_env_assertions")
    graded = await official._grade_tau2(request)
    await request.client.aclose()
    assert graded.checks[0].passed is True
    assert graded.score == 1.0
    # No communicate or NL judge was in this task's reward basis, so no judge calls are priced.
    assert graded.judgments == ()

    product = {"tau2": {"transcript": TRANSCRIPT, "termination_reason": "max_steps"}}
    request = _tau2_request(product, domain="mock", task_id="create_task_1_with_env_assertions")
    graded = await official._grade_tau2(request)
    await request.client.aclose()
    assert graded.checks[0].passed is False
    assert graded.score == 0.0


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
    assert outcome["calls"] == []
