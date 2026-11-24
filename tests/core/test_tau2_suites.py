import json

import httpx
import pytest
from parallax import official_suites as official
from parallax import tau2_bridge
from parallax.extensions import GradeRequest
from parallax.judge import JudgeConfig, JudgePanel
from parallax.task import task_from_record

TASKS = {
    "airline": [{"id": "0", "user_scenario": {"instructions": {"reason_for_call": "Cancel my flight"}}}],
    "retail": [{"id": "7", "user_scenario": {"instructions": {"reason_for_call": "Exchange my order"}}}],
    "telecom": [{"id": "t-1", "user_scenario": {"instructions": {"reason_for_call": "Fix my data"}}}],
}
SPLITS = {"airline": {"base": ["0"]}, "retail": {"base": ["7"]}, "telecom": {"base": ["t-1"]}}


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


def _tau2_request(product):
    monkeypatch_records = None
    del monkeypatch_records
    record = None
    # Build the record directly so the test does not fetch the dataset.
    from parallax.models import Area, Capability, Provenance, TaskRecord

    record = TaskRecord(
        id="tau2-airline-0",
        version=1,
        area=Area.SAAS,
        summary="tau2 airline task 0",
        prompt="policy",
        grading=official.TAU2_POLICY,
        requires=frozenset({Capability.MCP, Capability.MULTI_TURN}),
        maturity="regression",
        provenance=Provenance.CONVERTED,
        data={"domain": "airline", "upstream_task_id": "0", "policy_ref": "airline/policy.md@pin"},
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


@pytest.mark.asyncio
async def test_tau2_grade_sends_evidence_to_bridge_and_keeps_simulator_cost_out(monkeypatch):
    seen: dict[str, object] = {}

    async def fake_bridge(request, payload):
        seen.update(payload)
        return {"passed": True, "score": 1.0, "reward": 1.0, "reason": "ok", "calls": []}

    monkeypatch.setattr(official, "_tau2_bridge", fake_bridge)
    monkeypatch.setenv("PARALLAX_OFFICIAL_TAU2_ROOT", "/tmp/tau2")
    product = {
        "tau2": {
            "transcript": [{"role": "assistant", "content": "done"}],
            "tool_calls": [{"name": "cancel", "arguments": {}}],
            "final_db_state": {"reservations": []},
            "simulator_calls": [{"cost_usd": 9.99, "input_tokens": 100}],
        }
    }
    request = _tau2_request(product)
    graded = await official._grade_tau2(request)
    await request.client.aclose()
    assert graded.checks[0].passed is True
    assert graded.score == 1.0
    # Simulator spend is harness-side evidence only: no priced judgment enters the grade.
    assert graded.judgments == ()
    assert graded.unpriced_calls == 0
    assert seen["domain"] == "airline"
    assert seen["task_id"] == "0"
    assert "simulator_calls" not in seen


@pytest.mark.asyncio
async def test_tau2_grade_without_transcript_is_unjudged():
    request = _tau2_request({"tau2": {"transcript": [], "tool_calls": [], "final_db_state": {}}})
    graded = await official._grade_tau2(request)
    await request.client.aclose()
    assert graded.checks[0].passed is None
    assert "missing evidence" in graded.checks[0].detail


def test_tau2_bridge_builds_messages_from_transcript_and_tool_calls():
    messages = tau2_bridge._messages(
        {
            "transcript": [{"role": "user", "content": "hi"}],
            "tool_calls": [{"id": "c1", "name": "cancel", "arguments": {"id": "r1"}}],
        }
    )
    # Plain dicts are returned so the bridge stays importable without tau2 installed.
    assert messages[0] == {"role": "user", "content": "hi"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["name"] == "cancel"
