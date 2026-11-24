import hashlib
from pathlib import Path

import httpx
import pytest
from parallax import harbor_tasks as harbor
from parallax.extensions import GradeRequest
from parallax.judge import JudgeConfig, JudgePanel
from parallax.task import task_from_record

FIXTURES = Path(__file__).parent / "fixtures" / "harbor"


def test_parse_task_dir_reads_the_pinned_harbor_subset():
    spec = harbor.parse_task_dir(FIXTURES / "example-task")
    assert spec.upstream_id == "example-task"
    assert spec.instruction == "Write the answer to /app/answer.txt."
    assert spec.docker_image == "example/example-task:20260101"
    assert spec.dockerfile == "environment/Dockerfile"
    assert spec.network_mode == "no-network"
    assert spec.cpus == 2
    assert spec.memory_mb == 4096
    assert spec.verifier_timeout_s == 120
    assert spec.verifier_mode == "shared"
    expected = hashlib.sha256((FIXTURES / "example-task" / "tests" / "test.sh").read_bytes()).hexdigest()
    assert spec.tests_digest != expected  # digest covers the tests tree listing, not one file alone
    assert harbor.tests_digest(FIXTURES / "example-task" / "tests") == spec.tests_digest


def test_parse_task_dir_rejects_multi_step_tasks_loudly():
    with pytest.raises(harbor.HarborTaskError, match="multi-step"):
        harbor.parse_task_dir(FIXTURES / "multi-step-task")


def test_terminal_bench_tasks_from_directory_build_frozen_records(tmp_path):
    root = tmp_path / "tb2"
    root.mkdir()
    import shutil

    shutil.copytree(FIXTURES / "example-task", root / "example-task")
    records = list(harbor.terminal_bench_tasks(root=root))
    assert len(records) == 1
    record = records[0]
    assert record.id == "terminal-bench-2-example-task"
    assert record.grading == "terminal-bench-2-upstream-v1"
    assert record.prompt == "Write the answer to /app/answer.txt."
    assert set(record.data) == {
        "upstream_id",
        "task_dir",
        "docker_image",
        "dockerfile",
        "network_mode",
        "cpus",
        "memory_mb",
        "verifier_timeout_s",
        "tests_digest",
    }
    assert record.data["upstream_id"] == "example-task"
    assert record.data["network_mode"] == "no-network"


def _tb2_request(product, tmp_path):
    root = tmp_path / "tb2"
    root.mkdir(exist_ok=True)
    import shutil

    shutil.copytree(FIXTURES / "example-task", root / "example-task", dirs_exist_ok=True)
    record = list(harbor.terminal_bench_tasks(root=root))[0]
    task = task_from_record(harbor.TERMINAL_BENCH, record)
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
async def test_tb2_grade_uses_shared_container_verifier_reward(tmp_path):
    request = _tb2_request({"terminal_bench": {"reward": 1.0, "exit": 0, "mode": "shared"}}, tmp_path)
    graded = await harbor._grade(request)
    await request.client.aclose()
    assert graded.checks[0].passed is True
    assert graded.score == 1.0


@pytest.mark.asyncio
async def test_tb2_grade_without_evidence_is_unjudged(tmp_path):
    request = _tb2_request(None, tmp_path)
    graded = await harbor._grade(request)
    await request.client.aclose()
    assert graded.checks[0].passed is None
    assert "missing evidence" in graded.checks[0].detail


@pytest.mark.asyncio
async def test_tb2_snapshot_verify_path_is_used_when_no_shared_reward(tmp_path, monkeypatch):
    async def fake_verify(task, workspace):
        return {"reward": 0.0, "exit": 0, "mode": "snapshot"}

    monkeypatch.setattr(harbor, "_snapshot_verify", fake_verify)
    request = _tb2_request(None, tmp_path)
    workspace = tmp_path / "snapshot"
    workspace.mkdir()
    from types import SimpleNamespace

    request = GradeRequest(
        task=request.task,
        evidence=SimpleNamespace(workspace=workspace),  # type: ignore[arg-type]
        answers=request.answers,
        product=None,
        execution={},
        harness_model=None,
        panel=None,
        judge_config=None,
        client=request.client,
    )
    graded = await harbor._grade(request)
    await request.client.aclose()
    assert graded.checks[0].passed is False
    assert graded.score == 0.0
