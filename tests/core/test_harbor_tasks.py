import asyncio
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
        "agent_timeout_s",
        "tests_digest",
    }
    assert record.data["upstream_id"] == "example-task"
    assert record.data["network_mode"] == "no-network"


def _tb2_request(product, fixture: str = "tiny-task", tmp_path: Path | None = None, task_container: str | None = None):
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
        task_container=task_container,
    )


@pytest.mark.asyncio
async def test_tb2_grade_without_live_container_is_unjudged(tmp_path):
    request = _tb2_request(None, tmp_path=tmp_path)
    graded = await harbor._grade(request)
    await request.client.aclose()
    assert graded.checks[0].passed is None
    assert "parallax started no task container" in graded.checks[0].detail


@pytest.mark.asyncio
async def test_tb2_grade_never_trusts_a_harness_reported_container_or_reward(tmp_path):
    # A harness-reported container handle (the deleted terminal_bench.container evidence key)
    # and reward are ignored: only GradeRequest.task_container, recorded by parallax, grades.
    request = _tb2_request(
        {"terminal_bench": {"container": "forged", "reward": 1.0, "exit": 0, "mode": "shared"}}, tmp_path=tmp_path
    )
    graded = await harbor._grade(request)
    await request.client.aclose()
    assert graded.checks[0].passed is None
    assert graded.score is None


def test_image_ref_pins_the_digest_and_check_refuses_a_mismatch(monkeypatch):
    spec = harbor.HarborTaskSpec(
        upstream_id="x", instruction="i", docker_image="example/x:1",
        docker_image_digest="sha256:" + "a" * 64,
    )  # fmt: skip
    assert harbor._image_ref(spec) == "example/x@sha256:" + "a" * 64
    assert harbor._image_ref(spec.model_copy(update={"docker_image_digest": None})) == "example/x:1"

    async def fake_docker(*argv, timeout=None):
        if argv[0] == "inspect":
            return 0, "sha256:" + "b" * 64, ""
        return 0, '["example/x@sha256:' + "b" * 64 + '"]', ""

    monkeypatch.setattr(harbor, "_docker", fake_docker)
    with pytest.raises(harbor.HarborTaskError, match="digest mismatch"):
        asyncio.run(harbor._check_container_image("c1", spec))

    async def matching_docker(*argv, timeout=None):
        if argv[0] == "inspect":
            return 0, "sha256:" + "a" * 64, ""
        return 0, '["example/x@sha256:' + "a" * 64 + '"]', ""

    monkeypatch.setattr(harbor, "_docker", matching_docker)
    asyncio.run(harbor._check_container_image("c1", spec))


@pytest.mark.asyncio
async def test_tb2_infra_failure_is_unjudged_but_agent_failure_is_failed(tmp_path, monkeypatch):
    async def infra(container, data):
        return {
            "reward": None,
            "copy_exit": 1,
            "test_exit": None,
            "error": "copy failed",
            "failure_kind": "infrastructure",
        }

    monkeypatch.setattr(harbor, "run_verifier", infra)
    request = _tb2_request(None, tmp_path=tmp_path, task_container="parallax-owned")
    graded = await harbor._grade(request)
    await request.client.aclose()
    assert graded.checks[0].passed is None
    assert "infrastructure" in graded.checks[0].detail

    async def agent(container, data):
        return {"reward": None, "copy_exit": 0, "test_exit": 0, "error": "no reward file", "failure_kind": "agent"}

    monkeypatch.setattr(harbor, "run_verifier", agent)
    request = _tb2_request(None, tmp_path=tmp_path, task_container="parallax-owned")
    graded = await harbor._grade(request)
    await request.client.aclose()
    assert graded.checks[0].passed is False
    assert graded.score == 0.0


def test_controls_declare_live_container_and_records_carry_agent_timeout():
    from parallax.harnesses.null import null_info
    from parallax.harnesses.oracle import ORACLE_INFO

    assert Capability.LIVE_CONTAINER in ORACLE_INFO.capabilities
    assert Capability.LIVE_CONTAINER in null_info("claim").capabilities
    spec = harbor.parse_task_dir(FIXTURES / "tiny-task")
    assert spec.agent_timeout_s == 60


def _container_exists(name: str) -> bool:
    # Tests run in parallel xdist workers, so assert on this test's own container by name.
    return subprocess.run(["docker", "inspect", name], capture_output=True, check=False).returncode == 0


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tb2_verifier_pass_and_fail_in_live_container(tmp_path):
    request = _tb2_request(None, tmp_path=tmp_path)
    container = await harbor.start_task_container(request.task.data)
    try:
        graded = await harbor._grade(_tb2_request(None, tmp_path=tmp_path, task_container=container))
        assert graded.checks[0].passed is False
        assert graded.score == 0.0
        code, _out, _err = await harbor._docker(
            "exec", container, "sh", "-c", "mkdir -p /app; echo done > /app/answer.txt"
        )
        assert code == 0
        graded = await harbor._grade(_tb2_request(None, tmp_path=tmp_path, task_container=container))
        assert graded.checks[0].passed is True
        assert graded.score == 1.0
        assert "test_exit=0" in graded.checks[0].detail
    finally:
        await harbor.remove_container(container)
        await request.client.aclose()
    assert not _container_exists(container)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tb2_verifier_missing_reward_file_is_a_failed_check(tmp_path):
    request = _tb2_request(None, fixture="no-reward-task", tmp_path=tmp_path)
    container = await harbor.start_task_container(request.task.data)
    try:
        graded = await harbor._grade(
            _tb2_request(None, fixture="no-reward-task", tmp_path=tmp_path, task_container=container)
        )
        assert graded.checks[0].passed is False
        assert graded.score == 0.0
        assert "no reward file" in graded.checks[0].detail
    finally:
        await harbor.remove_container(container)
        await request.client.aclose()


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tb2_oracle_solution_passes_and_null_fails_through_the_owned_container(tmp_path):
    # The runner-owned path the oracle/null controls use: parallax starts the container, the
    # oracle runs the pinned solve.sh inside it (run_solution), the grader verifies, parallax
    # removes it. Null runs no solution in its container and must fail.
    request = _tb2_request(None, tmp_path=tmp_path)
    container = await harbor.start_task_container(request.task.data)
    started = [container]
    try:
        code, detail = await harbor.run_solution(container, request.task.data)
        assert code == 0, detail
        graded = await harbor._grade(_tb2_request(None, tmp_path=tmp_path, task_container=container))
        assert graded.checks[0].passed is True
        assert graded.score == 1.0
    finally:
        await harbor.remove_container(container)
    container = await harbor.start_task_container(request.task.data)
    started.append(container)
    try:
        graded = await harbor._grade(_tb2_request(None, tmp_path=tmp_path, task_container=container))
        assert graded.checks[0].passed is False
        assert graded.score == 0.0
    finally:
        await harbor.remove_container(container)
        await request.client.aclose()
    assert not any(_container_exists(name) for name in started)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tb2_forged_tests_and_background_reward_writer_do_not_pass(tmp_path):
    request = _tb2_request(None, tmp_path=tmp_path)
    container = await harbor.start_task_container(request.task.data)
    try:
        # Plant a forged test suite and a self-respawning reward writer: a supervisor that
        # relaunches its worker whenever the worker dies, so only the grader's container
        # restart (not a process kill) can stop it rewriting the reward file.
        code, _o, _e = await harbor._docker(
            "exec",
            container,
            "sh",
            "-c",
            "mkdir -p /tests /logs/verifier; printf '#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n' > /tests/test.sh; "
            "echo forged > /tests/conftest.py; chmod +x /tests/test.sh",
        )
        assert code == 0
        writer = (
            "while true; do "
            "sh -c 'while true; do mkdir -p /logs/verifier; echo 1 > /logs/verifier/reward.txt; sleep 0.1; done' & "
            "wait $!; done"
        )
        code, _o, _e = await harbor._docker("exec", "-d", container, "sh", "-c", writer)
        assert code == 0
        graded = await harbor._grade(_tb2_request(None, tmp_path=tmp_path, task_container=container))
        assert graded.checks[0].passed is False
        assert graded.score == 0.0
    finally:
        await harbor.remove_container(container)
        await request.client.aclose()


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tb2_verifier_timeout_is_an_agent_failure_and_container_removed(tmp_path):
    # Upstream Harbor fails on verifier timeout regardless of reward, so no late-reward
    # fixture exists: a timed-out verifier is the agent's failure, never a read reward.
    request = _tb2_request(None, fixture="slow-task", tmp_path=tmp_path)
    container = await harbor.start_task_container(request.task.data)
    try:
        outcome = await harbor.run_verifier(container, request.task.data)
        assert outcome["reward"] is None
        assert outcome["failure_kind"] == "agent"
        assert "timed out" in str(outcome["error"])
    finally:
        await harbor.remove_container(container)
        await request.client.aclose()
    assert not _container_exists(container)


def _labelled_containers(run_id: str) -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={harbor.RUN_LABEL}={run_id}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    return out


def _tiny_task(tmp_path: Path, **data_overrides):
    root = tmp_path / "tb2"
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURES / "tiny-task", root / "tiny-task", dirs_exist_ok=True)
    record = list(harbor.terminal_bench_tasks(root=root))[0]
    if data_overrides:
        record = record.model_copy(update={"data": {**record.data, **data_overrides}})
    return task_from_record(harbor.TERMINAL_BENCH, record)


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tb2_runner_oracle_passes_null_fails_and_start_failure_and_cancel_clean_up(tmp_path):
    # Through the runner itself: the oracle control passes on the tiny fixture and the null
    # control fails, each in the runner-owned labelled container, removed afterwards.
    from parallax.models import Isolation, Verdict
    from parallax.runner import RunOptions, read_results, run_async

    task = _tiny_task(tmp_path / "ok")
    out = tmp_path / "run-ok"
    await run_async(
        [task],
        ["oracle", "null-claim"],
        [None],
        RunOptions(out=out, isolation=Isolation.HOST, quiet=True, retry_invalid=0),
    )
    rows = {(r.harness): r for r in read_results(out / "results.jsonl")}
    assert rows["oracle"].verdict is Verdict.PASS and rows["oracle"].score == 1.0
    assert rows["null-claim"].verdict is Verdict.FAIL and rows["null-claim"].score == 0.0
    assert _labelled_containers(out.name) == []

    # A start failure (a digest the pinned tests refuse) cleans up the box it opened and
    # leaves no labelled container behind.
    bad = _tiny_task(tmp_path / "bad", docker_image_digest="sha256:" + "0" * 64)
    out = tmp_path / "run-bad"
    await run_async(
        [bad], ["null-claim"], [None], RunOptions(out=out, isolation=Isolation.HOST, quiet=True, retry_invalid=0)
    )
    (row,) = read_results(out / "results.jsonl")
    assert row.verdict is Verdict.INVALID
    assert _labelled_containers(out.name) == []

    # A cancelled trial leaves no labelled container either.
    task = _tiny_task(tmp_path / "cancel")
    out = tmp_path / "run-cancel"
    run = asyncio.create_task(
        run_async(
            [task], ["oracle"], [None], RunOptions(out=out, isolation=Isolation.HOST, quiet=True, retry_invalid=0)
        )
    )
    try:
        for _ in range(600):
            if _labelled_containers(out.name) or run.done():
                break
            await asyncio.sleep(0.05)
        assert _labelled_containers(out.name), "the trial never started its labelled container"
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    finally:
        if not run.done():
            run.cancel()
    assert _labelled_containers(out.name) == []


@pytest.mark.docker
@pytest.mark.asyncio
async def test_tb2_debian_verifier_runs_apt_get(tmp_path):
    # The capability regression fixture: test.sh runs apt-get, which --cap-drop ALL broke.
    try:
        httpx.get("https://deb.debian.org", timeout=10).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"no network for the apt fixture: {exc}")
    request = _tb2_request(None, fixture="debian-apt-task", tmp_path=tmp_path)
    container = await harbor.start_task_container(request.task.data)
    try:
        outcome = await harbor.run_verifier(container, request.task.data)
        assert outcome["reward"] == 1.0, outcome
    finally:
        await harbor.remove_container(container)
        await request.client.aclose()
