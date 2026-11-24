import hashlib
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from parallax import harbor_tasks as harbor
from parallax.extensions import GradeRequest
from parallax.judge import JudgeConfig, JudgePanel
from parallax.models import Capability
from parallax.task import task_from_record

FIXTURES = Path(__file__).parent / "fixtures" / "harbor"


def test_parse_task_dir_reads_the_pinned_harbor_subset():
    spec = harbor.parse_task_dir(FIXTURES / "example-task")
    assert spec.upstream_id == "example-task"
    assert spec.instruction == "Write the answer to /app/answer.txt."
    assert spec.docker_image == "example/example-task:20260101"
    assert spec.docker_image_digest is None  # not a pinned TB2 image, so no digest pin
    assert spec.dockerfile == "environment/Dockerfile"
    assert spec.network_mode == "no-network"
    assert spec.cpus == 2
    assert spec.memory_mb == 4096
    assert spec.pids_limit == harbor.DEFAULT_PIDS_LIMIT
    assert spec.verifier_timeout_s == 120
    expected = hashlib.sha256((FIXTURES / "example-task" / "tests" / "test.sh").read_bytes()).hexdigest()
    assert spec.tests_digest != expected  # digest covers the tests tree listing, not one file alone
    assert harbor.tests_digest(FIXTURES / "example-task" / "tests") == spec.tests_digest


def test_pinned_tb2_images_resolve_to_registry_digests():
    assert len(harbor.PINNED_IMAGE_DIGESTS) == harbor.TERMINAL_BENCH_EXPECTED_COUNT
    digest = harbor.PINNED_IMAGE_DIGESTS["alexgshaw/fix-git:20251031"]
    assert digest.startswith("sha256:")
    spec = harbor.parse_task_dir(FIXTURES / "tiny-task")
    assert spec.docker_image == "alpine:3.20"
    assert spec.docker_image_digest is None


def test_parse_task_dir_rejects_multi_step_tasks_loudly():
    with pytest.raises(harbor.HarborTaskError, match="multi-step"):
        harbor.parse_task_dir(FIXTURES / "multi-step-task")


def test_terminal_bench_tasks_from_directory_build_frozen_records(tmp_path):
    root = tmp_path / "tb2"
    root.mkdir()
    shutil.copytree(FIXTURES / "example-task", root / "example-task")
    records = list(harbor.terminal_bench_tasks(root=root))
    assert len(records) == 1
    record = records[0]
    assert record.id == "terminal-bench-2-example-task"
    assert record.grading == "terminal-bench-2-upstream-v1"
    assert record.prompt == "Write the answer to /app/answer.txt."
    assert Capability.LIVE_CONTAINER in record.requires
    assert set(record.data) == {
        "upstream_id",
        "task_dir",
        "docker_image",
        "docker_image_digest",
        "dockerfile",
        "network_mode",
        "cpus",
        "memory_mb",
        "pids_limit",
        "verifier_timeout_s",
        "tests_digest",
    }
    assert record.data["upstream_id"] == "example-task"
    assert record.data["network_mode"] == "no-network"


def _tb2_request(product, fixture: str = "tiny-task", tmp_path: Path | None = None):
    root = (tmp_path or Path("/tmp")) / "tb2"
    root.mkdir(exist_ok=True)
    shutil.copytree(FIXTURES / fixture, root / fixture, dirs_exist_ok=True)
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
async def test_tb2_grade_without_live_container_is_unjudged(tmp_path):
    request = _tb2_request(None, tmp_path=tmp_path)
    graded = await harbor._grade(request)
    await request.client.aclose()
    assert graded.checks[0].passed is None
    assert "no live task container" in graded.checks[0].detail


@pytest.mark.asyncio
async def test_tb2_grade_never_trusts_a_harness_reported_reward(tmp_path):
    request = _tb2_request({"terminal_bench": {"reward": 1.0, "exit": 0, "mode": "shared"}}, tmp_path=tmp_path)
    graded = await harbor._grade(request)
    await request.client.aclose()
    assert graded.checks[0].passed is None
    assert graded.score is None


def _leftover_containers() -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=parallax-tb2"], capture_output=True, text=True, check=False
    ).stdout.split()
    return out


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tb2_verifier_pass_and_fail_in_live_container(tmp_path):
    request = _tb2_request(None, tmp_path=tmp_path)
    container = await harbor.start_task_container(request.task.data)
    try:
        # No answer yet: the real verifier runs and rewards 0.
        graded = await harbor._grade(_tb2_request({"terminal_bench": {"container": container}}, tmp_path=tmp_path))
        assert graded.checks[0].passed is False
        assert graded.score == 0.0
        code, _out, _err = await harbor._docker(
            "exec", container, "sh", "-c", "mkdir -p /app; echo done > /app/answer.txt"
        )
        assert code == 0
        graded = await harbor._grade(_tb2_request({"terminal_bench": {"container": container}}, tmp_path=tmp_path))
        assert graded.checks[0].passed is True
        assert graded.score == 1.0
        assert "test_exit=0" in graded.checks[0].detail
    finally:
        await harbor.remove_container(container)
        await request.client.aclose()
    assert container not in _leftover_containers()


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tb2_verifier_missing_reward_file_is_a_failed_check(tmp_path):
    request = _tb2_request(None, fixture="no-reward-task", tmp_path=tmp_path)
    container = await harbor.start_task_container(request.task.data)
    try:
        graded = await harbor._grade(
            _tb2_request({"terminal_bench": {"container": container}}, fixture="no-reward-task", tmp_path=tmp_path)
        )
        assert graded.checks[0].passed is False
        assert graded.score == 0.0
        assert "no reward file" in graded.checks[0].detail
    finally:
        await harbor.remove_container(container)
        await request.client.aclose()


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tb2_oracle_passes_null_fails_and_containers_are_removed(tmp_path):
    oracle_request = _tb2_request(None, tmp_path=tmp_path)
    try:
        oracle = await harbor.oracle_conformance(oracle_request.task.data)
        assert oracle["passed"] is True
        assert oracle["reward"] == 1.0
        null = await harbor.null_conformance(oracle_request.task.data)
        assert null["passed"] is False
        assert null["reward"] == 0.0
    finally:
        await oracle_request.client.aclose()
    assert _leftover_containers() == []


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tb2_verifier_timeout_is_recorded_and_container_removed(tmp_path):
    request = _tb2_request(None, fixture="slow-task", tmp_path=tmp_path)
    try:
        outcome = await harbor.oracle_conformance(request.task.data)
        assert outcome["passed"] is False
        assert "timed out" in str(outcome["error"])
    finally:
        await request.client.aclose()
    assert _leftover_containers() == []
