"""Runner over the fixture tasks in host isolation: controls score as designed, resume is keyed by fingerprint."""

from __future__ import annotations

import asyncio
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from parallax import provenance, runner
from parallax.box import BoxError
from parallax.models import (
    ApprovalAnswer,
    ApprovalDecision,
    Capability,
    CheckResult,
    CostSource,
    Evidence,
    FailureMode,
    Fire,
    HarnessInfo,
    HarnessKind,
    Isolation,
    RemoteBox,
    RunOrigin,
    Task,
    Trajectory,
    TrajectoryStatus,
    Trial,
    TrialFlag,
    TrialResult,
    TurnInput,
    TurnOutput,
    TurnStatus,
    Verdict,
    WebIsolation,
    WorldEvidence,
)
from parallax.protocols import BoxHandle
from parallax.runner import (
    HarnessPlan,
    RunOptions,
    decide,
    finish_uncancelled,
    latest_results,
    model_flags,
    only_pairs,
    plan_trials,
    probe_tasks,
    read_results,
    result_row,
    retry_transient_invalid,
    run_async,
    start_order,
    transient_invalid,
    turn_finals,
)
from parallax.semantic import Graded
from parallax.task import discover, load_task

TASKS = Path(__file__).parent / "fixtures" / "tasks"


def saved_evidence(task: Task, trial_dir: Path) -> Evidence:
    """Evidence rebuilt from the files the runner wrote for the trial."""
    traj = Trajectory.model_validate_json((trial_dir / "trajectory.json").read_text())
    return Evidence(
        delivered_turns=traj.delivered_turns, answers=traj.answers, finals=turn_finals(traj.answers, traj.steps),
        world=WorldEvidence.model_validate_json((trial_dir / "world_evidence.json").read_text()),
        workspace=None, turn_workspaces={}, seed_files=None, verify={},
        persona_tz=task.seed.user.timezone, approvals=traj.approvals,
    )  # fmt: skip


def opts(out: Path) -> RunOptions:
    return RunOptions(out=out, isolation=Isolation.HOST, quiet=True)


async def test_unjudged_runs_keep_evidence_without_quality_verdicts(tmp_path: Path) -> None:
    options = opts(tmp_path / "r").model_copy(update={"provenance_extra": {"product.served_commit": "abc123"}})
    run_dir = await run_async(discover([TASKS]), ["oracle", "null-claim", "null-refuse"], [None], options)
    verdicts = {(r.task_id, r.harness): r.verdict for r in read_results(run_dir / "results.jsonl")}
    assert len(verdicts) == 9
    for (task_id, harness), verdict in verdicts.items():
        want = Verdict.INVALID
        assert verdict is want, (task_id, harness)
    recorded = provenance.read(run_dir)
    assert recorded.extra == {"product.served_commit": "abc123"}
    assert set(recorded.task_hashes) == {t.id for t in discover([TASKS])}


async def test_resume_skips_completed_and_rotates_on_fingerprint_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def canned_grade(*args: object, **kwargs: object) -> Graded:
        return Graded(checks=(check(True),), same_family=False)

    monkeypatch.setattr(runner, "grade_checks", canned_grade)  # resume protocol, no model-quality claim
    tasks = [t for t in discover([TASKS]) if t.id == "mini-mail"]
    out = tmp_path / "r"
    await run_async(tasks, ["null-claim"], [None], opts(out))
    await run_async(tasks, ["null-claim"], [None], opts(out))
    assert len(read_results(out / "results.jsonl")) == 1  # the second run found nothing to do

    # The canned judge keeps this resume test offline.
    monkeypatch.setenv("PARALLAX_JUDGE_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PARALLAX_JUDGE_API_KEY", "unused")
    changed = opts(out).model_copy(update={"judge": ("some/judge",)})  # the judge is part of the key
    await run_async(tasks, ["null-claim"], [None], changed)
    assert len(read_results(out / "results.jsonl.stale")) == 1
    assert len(read_results(out / "results.jsonl")) == 1


def check(passed: bool | None, *, gate: bool = True, advisory: bool = False) -> CheckResult:
    return CheckResult(id="c", kind="state", mode=FailureMode.MISSING_ACTION, gate=gate, advisory=advisory,
                       weight=1.0, passed=passed, detail="")  # fmt: skip


@pytest.mark.parametrize(
    ("na", "invalid", "status", "checks", "want"),
    [
        ("requires memory", ["world internal error"], TrajectoryStatus.COMPLETED, [check(False)], Verdict.NA),
        (None, ["meter unreachable"], TrajectoryStatus.TIMEOUT, [check(False)], Verdict.INVALID),
        (None, [], TrajectoryStatus.COMPLETED, [check(None)], Verdict.INVALID),
        (None, [], TrajectoryStatus.BOOT_ERROR, [], Verdict.INVALID),
        (None, [], TrajectoryStatus.HARNESS_ERROR, [check(True)], Verdict.INVALID),
        (None, [], TrajectoryStatus.TIMEOUT, [check(True)], Verdict.INVALID),
        (None, [], TrajectoryStatus.OVER_BUDGET, [check(True)], Verdict.INVALID),
        (None, [], TrajectoryStatus.COMPLETED, [check(True), check(False)], Verdict.FAIL),
        (None, [], TrajectoryStatus.COMPLETED, [check(True), check(False, gate=False)], Verdict.PASS),
        (None, [], TrajectoryStatus.COMPLETED, [check(True), check(None, advisory=True)], Verdict.PASS),
    ],
)
def test_verdict_precedence(
    na: str | None, invalid: list[str], status: TrajectoryStatus, checks: list[CheckResult], want: Verdict
) -> None:
    assert decide(na, invalid, status, checks)[0] is want


INFO = HarnessInfo(name="h", kind=HarnessKind.OPEN, capabilities=frozenset(Capability), pins_model=True,
                   cost_source=CostSource.METERED, isolation=Isolation.HOST, version_cmd=("h",))  # fmt: skip


@pytest.mark.parametrize(
    ("actual", "unobserved", "flags"),
    [
        (("claude-x-20250101",), False, set()),
        ((), False, {TrialFlag.MODEL_UNOBSERVED}),  # no response named a model: the request is not evidence
        (("claude-x",), True, {TrialFlag.MODEL_UNOBSERVED}),  # one response named none: a proxy could swap it
        (("claude-y",), False, {TrialFlag.MODEL_MISMATCH}),
    ],
)
def test_only_response_named_models_confirm_the_model(
    actual: tuple[str, ...], unobserved: bool, flags: set[TrialFlag]
) -> None:
    assert model_flags("anthropic/claude-x", actual, unobserved) == flags


def test_resume_key_changes_with_isolation() -> None:
    tasks = [t for t in discover([TASKS]) if t.id == "mini-mail"]

    def key(isolation: Isolation) -> str:
        hp = HarnessPlan(name="h", info=INFO, version="1", config_hash="c")
        (plan,) = plan_trials(tasks, [hp], ["m/x"], RunOptions(out=Path("unused"), isolation=isolation))
        return plan.key

    assert key(Isolation.HOST) != key(Isolation.DOCKER)


def test_trials_start_longest_first_with_models_side_by_side() -> None:
    """A trial's turns (steps and scheduled fires) set its expected length. The longest start first,
    so they do not set the run's tail; equal tasks keep their plan order."""
    tasks = {t.id: t for t in discover([TASKS])}
    fire = Fire(name="fire-1", at=datetime(2026, 1, 1, tzinfo=UTC), job={})
    scheduled = tasks["mini-mail"].model_copy(
        update={"meta": tasks["mini-mail"].meta.model_copy(update={"id": "mini-fires", "fires": (fire, fire)})}
    )
    hp = HarnessPlan(name="h", info=INFO, version="1", config_hash="c")
    ordered = [tasks["mini-mail"], tasks["mini-repo"], tasks["mini-steps"], scheduled]  # 1, 1, 2 and 3 turns
    plans = plan_trials(ordered, [hp], ["m/flash", "m/ultra"], RunOptions(out=Path("unused"), isolation=Isolation.HOST))

    started = start_order(plans)

    assert [(p.task.id, p.model) for p in started] == [
        ("mini-fires", "m/flash"), ("mini-fires", "m/ultra"),
        ("mini-steps", "m/flash"), ("mini-steps", "m/ultra"),
        ("mini-mail", "m/flash"), ("mini-mail", "m/ultra"),
        ("mini-repo", "m/flash"), ("mini-repo", "m/ultra"),
    ]  # fmt: skip
    assert sorted(p.key for p in started) == sorted(p.key for p in plans)


def test_pairs_keep_only_their_trials_in_planned_order_and_refuse_an_unplanned_pair() -> None:
    tasks = sorted(discover([TASKS]), key=lambda t: t.id)[:2]
    a, b = (t.id for t in tasks)
    hp = HarnessPlan(name="h", info=INFO, version="1", config_hash="c")
    plans = plan_trials(tasks, [hp], ["m/x", "m/y"], RunOptions(out=Path("unused")))
    assert only_pairs(plans, ()) == plans
    kept = only_pairs(plans, [(b, "m/x"), (a, "m/y")])
    assert [(p.task.id, p.model) for p in kept] == [(p.task.id, p.model) for p in plans if p in kept]
    assert {(p.task.id, p.model) for p in kept} == {(b, "m/x"), (a, "m/y")}
    with pytest.raises(ValueError, match=f"{a}=m/z"):
        only_pairs(plans, [(a, "m/z")])


class Crashing:
    """An unmetered harness whose only turn is a harness_error, before or after answering."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.info = INFO.model_copy(update={"name": "crashing", "cost_source": CostSource.NONE})

    async def start(self, trial: Trial, box: BoxHandle) -> None:
        return None

    async def turn(self, t: TurnInput) -> TurnOutput:
        return TurnOutput(status=TurnStatus.HARNESS_ERROR, answer=self.answer, session_id=None, error="boom")

    async def stop(self) -> None:
        return None

    async def version(self, box: BoxHandle) -> str:
        return "1"


@pytest.mark.parametrize(("answer", "want"), [("", Verdict.INVALID), ("Partly done.", Verdict.INVALID)])
async def test_unattributed_harness_error_stays_invalid_regardless_of_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str, want: Verdict
) -> None:
    monkeypatch.setattr(runner, "load_harness", lambda name: Crashing(answer))
    tasks = [t for t in discover([TASKS]) if t.id == "mini-mail"]
    run_dir = await run_async(tasks, ["crashing"], ["m/x"], opts(tmp_path / "r"))
    (row,) = read_results(run_dir / "results.jsonl")
    assert row.verdict is want
    assert TrialFlag.MODEL_UNOBSERVED in row.flags
    assert row.dir is not None
    assert "status: harness_error" in (run_dir / row.dir / "harness.log").read_text()


class Signed(Crashing):
    """A product login: it names its account before the world exists, and the trial runs as that user."""

    def __init__(self) -> None:
        super().__init__("Done.")
        self.order: list[str] = []
        self.user_email: str | None = None

    async def account(self, trial_id: str) -> str | None:
        self.order.append("account")
        return "px-7@product.test"

    async def start(self, trial: Trial, box: BoxHandle) -> None:
        self.order.append("start")
        self.user_email = trial.user.email

    async def turn(self, t: TurnInput) -> TurnOutput:
        return TurnOutput(status=TurnStatus.COMPLETED, answer=self.answer, session_id=None)

    async def stop(self) -> None:
        self.order.append("stop")


async def test_an_account_is_released_when_the_trial_setup_fails_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Signed()
    monkeypatch.setattr(runner, "load_harness", lambda name: harness)

    async def no_meter(*args: object) -> Trial:
        raise RuntimeError("meter registration failed")

    monkeypatch.setattr(runner.TrialExecution, "trial_input", no_meter)
    tasks = [t for t in discover([TASKS]) if t.id == "mini-mail"]
    await run_async(tasks, ["signed"], ["m/x"], opts(tmp_path / "r"))
    assert harness.order[:2] == ["account", "stop"]  # each attempt releases the account it minted
    assert harness.order.count("account") == harness.order.count("stop")


async def test_an_account_bound_harness_runs_the_world_as_its_own_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Signed()
    monkeypatch.setattr(runner, "load_harness", lambda name: harness)
    tasks = [t for t in discover([TASKS]) if t.id == "mini-mail"]
    run_dir = await run_async(tasks, ["signed"], ["m/x"], opts(tmp_path / "r"))
    assert harness.order == ["account", "start", "stop"]
    assert harness.user_email == "px-7@product.test"
    (row,) = read_results(run_dir / "results.jsonl")
    assert row.task_hash == tasks[0].hash
    assert row.dir is not None
    world = WorldEvidence.model_validate_json((run_dir / row.dir / "world_evidence.json").read_text())
    assert {tuple(m["to"]) for m in world.seed_state["mail.messages"].values()} == {("px-7@product.test",)}


class Raising(Crashing):
    async def turn(self, t: TurnInput) -> TurnOutput:
        raise RuntimeError("adapter bug")


async def test_a_turn_whose_adapter_raised_still_counts_toward_agent_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "load_harness", lambda name: Raising(""))
    tasks = [t for t in discover([TASKS]) if t.id == "mini-mail"]
    run_dir = await run_async(tasks, ["crashing"], ["m/x"], opts(tmp_path / "r"))
    (row,) = read_results(run_dir / "results.jsonl")
    assert row.agent_ms is not None


class Hanging(Crashing):
    async def turn(self, t: TurnInput) -> TurnOutput:
        await asyncio.sleep(60)
        raise AssertionError("the turn deadline should have cancelled this")


async def test_a_turn_past_the_turn_timeout_ends_the_trial_as_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Enforced by the runner, so a plugin harness that never returns is bounded too.
    monkeypatch.setattr(runner, "load_harness", lambda name: Hanging(""))
    tasks = [t for t in discover([TASKS]) if t.id == "mini-mail"]
    options = opts(tmp_path / "r").model_copy(update={"turn_timeout_s": 0.5})
    started = time.monotonic()
    run_dir = await asyncio.wait_for(run_async(tasks, ["crashing"], ["m/x"], options), 30)
    (row,) = read_results(run_dir / "results.jsonl")
    assert time.monotonic() - started < 30
    assert row.status is TrajectoryStatus.TIMEOUT


async def test_a_pinned_world_port_is_served_and_a_taken_one_is_an_environment_error() -> None:
    async with runner.services(Isolation.REMOTE, None, False, "port-test", world_port=0) as probe:
        port = int(probe.world_local.rsplit(":", 1)[1])
    async with runner.services(Isolation.REMOTE, None, False, "port-test", world_port=port) as svc:
        assert svc.world_local == f"http://127.0.0.1:{port}"
        with pytest.raises(BoxError, match=f"{port}.*taken"):
            async with runner.services(Isolation.REMOTE, None, False, "port-test-2", world_port=port):
                pass


async def test_harness_cleanup_outlives_a_cancelled_trial() -> None:
    """SIGINT cancels the trial mid-`stop`: the stop still runs to its end, and the cancel still lands."""
    stopping, released, done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stop() -> None:
        stopping.set()
        await released.wait()
        done.set()

    trial = asyncio.create_task(finish_uncancelled(stop()))
    await stopping.wait()
    trial.cancel()
    await asyncio.sleep(0)
    trial.cancel()  # a second Ctrl-C
    await asyncio.sleep(0)
    assert not trial.done()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await trial
    assert done.is_set()


@pytest.mark.parametrize(("remote_box", "kinds"), [(RemoteBox.DEVICE, 1), (RemoteBox.HOST, 1)])
def test_a_harness_is_probed_once_in_the_one_box_kind_its_trials_use(remote_box: RemoteBox, kinds: int) -> None:
    info = HarnessInfo(
        name="remote", kind=next(iter(HarnessKind)), capabilities=frozenset(), pins_model=True,
        cost_source=CostSource.HARNESS, isolation=Isolation.REMOTE, remote_box=remote_box,
    )  # fmt: skip
    probed = probe_tasks(discover([TASKS]), info, Isolation.REMOTE)
    assert len(probed) == kinds


async def test_delivered_scheduled_prompt_survives_saved_evidence(tmp_path):
    from parallax.semantic import evidence_state
    from parallax.task import load_task

    task = load_task(Path("tasks/core/schedule-one-off-reminder"))
    out = await run_async([task], ["oracle"], [None], opts(tmp_path / "run"))
    row = read_results(out / "results.jsonl")[0]
    ev = saved_evidence(task, out / row.dir)
    assert len(ev.delivered_turns) == len(task.turns) + len(task.meta.fires)
    fire = ev.delivered_turns[-1]
    assert fire.text.startswith('Scheduled task "') and "Instructions you saved:" in fire.text
    assert evidence_state(task, ev)["evidence"]["delivered_turns"][-1]["text"] == fire.text


# A remote product whose trials each get a device box, as a plugin harness declares it.
DEVICE_INFO = HarnessInfo(
    name="device-product",
    kind=HarnessKind.PRODUCT,
    capabilities=frozenset({Capability.MCP, Capability.WORKSPACE, Capability.DESKTOP}),
    pins_model=False,
    cost_source=CostSource.HARNESS,
    isolation=Isolation.REMOTE,
    version_cmd=("product", "--version"),
    remote_box=RemoteBox.DEVICE,
)


def test_device_harness_probes_the_desktop_image_separately() -> None:
    from parallax.task import load_task

    tasks = discover([TASKS])
    tasks.append(load_task(Path("tasks/core/files-desktop-fill-expense-form")))
    probed = probe_tasks(tasks, DEVICE_INFO, Isolation.REMOTE)
    assert len(probed) == 2
    assert sum(Capability.DESKTOP in task.meta.requires for task in probed.values()) == 1


async def test_a_failed_desktop_probe_voids_only_desktop_trials(monkeypatch: pytest.MonkeyPatch) -> None:
    from parallax.box import BoxKind
    from parallax.task import load_task

    class Probed:
        info = DEVICE_INFO

        async def version(self, box: object) -> str:
            return "1"

    class Closing:
        async def close(self) -> None:
            return None

    async def open_box(task, *args):
        if Capability.DESKTOP in task.meta.requires:
            raise BoxError("desktop image is amd64-only")
        return Closing()

    monkeypatch.setattr(runner, "load_harness", lambda _: Probed())
    monkeypatch.setattr(runner, "open_box", open_box)
    tasks = [*discover([TASKS]), load_task(Path("tasks/core/files-desktop-fill-expense-form"))]
    hp = await runner.plan_harness("device-product", tasks, Isolation.REMOTE, None)
    assert hp.unavailable is None
    assert hp.version == "1"
    assert dict(hp.unavailable_boxes) == {BoxKind.DESKTOP: "desktop image is amd64-only"}


async def test_missing_desktop_capability_is_na_even_when_harness_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parallax.task import load_task

    info = INFO.model_copy(update={"capabilities": INFO.capabilities - {Capability.DESKTOP}})

    async def unavailable(*args, **kwargs):
        return HarnessPlan(name="h", info=info, version=None, config_hash="c", unavailable="not installed")

    monkeypatch.setattr(runner, "plan_harness", unavailable)
    harness = Crashing("")
    harness.info = info
    monkeypatch.setattr(runner, "load_harness", lambda _: harness)
    task = load_task(Path("tasks/core/files-desktop-fill-expense-form"))
    out = await run_async([task], ["h"], ["m/x"], opts(tmp_path / "run"))
    [row] = read_results(out / "results.jsonl")
    assert row.verdict is Verdict.NA
    assert row.na_reason == "requires desktop"


class Approving(Crashing):
    """Answers every turn and reports the approval card a job step raised, as a product harness does."""

    def __init__(self) -> None:
        super().__init__("Done.")
        self.info = INFO.model_copy(
            update={"name": "approving", "cost_source": CostSource.NONE, "capabilities": frozenset(Capability)}
        )
        self.delivered: list[TurnInput] = []

    async def turn(self, t: TurnInput) -> TurnOutput:
        self.delivered.append(t)
        approvals = () if t.approval is None else (ApprovalAnswer(turn=t.index, request="run it", decision=t.approval),)
        return TurnOutput(status=TurnStatus.COMPLETED, answer="Done.", session_id=None, approvals=approvals)


async def test_a_job_step_reaches_the_harness_and_its_cards_become_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tasks" / "mini-steps"
    shutil.copytree(TASKS / "mini-steps", root)
    toml = root / "task.toml"
    toml.write_text(
        toml.read_text()
        .replace('["mcp", "multi_turn"]', '["mcp", "multi_turn", "unattended"]')
        .replace('patch = "sam-joins"', 'patch = "sam-joins"\nsession = "new"\norigin = "job"\napproval = "deny"')
    )
    task = load_task(root)
    harness = Approving()
    monkeypatch.setattr(runner, "load_harness", lambda name: harness)
    run_dir = await run_async([task], ["approving"], ["m/x"], opts(tmp_path / "r"))
    assert [(t.origin, t.approval) for t in harness.delivered] == [
        (RunOrigin.MEMBER, None),
        (RunOrigin.JOB, ApprovalDecision.DENY),
    ]
    (row,) = read_results(run_dir / "results.jsonl")
    assert row.dir is not None
    ev = saved_evidence(task, run_dir / row.dir)
    assert ev.approvals == (ApprovalAnswer(turn=2, request="run it", decision=ApprovalDecision.DENY),)
    assert ev.delivered_turns[1].origin is RunOrigin.JOB
    # A job task shows its card count to the judges even when it is zero.
    from parallax.semantic import evidence_state

    assert evidence_state(task, ev.model_copy(update={"approvals": ()}))["evidence"]["approvals"] == []


async def test_runner_preserves_each_model_reported_by_a_multi_model_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = ("inference_net:deepseek-v4.1-flash-1017", "openrouter:anthropic/claude-opus-5.5")

    class MultiModel(Crashing):
        async def turn(self, t: TurnInput) -> TurnOutput:
            return TurnOutput(status=TurnStatus.COMPLETED, answer="Done.", session_id=None, actual_models=models)

    monkeypatch.setattr(runner, "load_harness", lambda name: MultiModel(""))
    tasks = [t for t in discover([TASKS]) if t.id == "mini-mail"]
    run_dir = await run_async(tasks, ["crashing"], ["m/x"], opts(tmp_path / "r"))
    (row,) = read_results(run_dir / "results.jsonl")
    assert row.actual_models == models


def _row(tmp_path: Path, verdict: Verdict, reason: str | None) -> TrialResult:
    """One result row for the first fixture task, flattened so a verdict/reason can be asserted on."""
    (plan,) = plan_trials(
        discover([TASKS])[:1],
        [HarnessPlan(name="h", info=INFO, version="1", config_hash="c")],
        ["m/x"],
        RunOptions(out=tmp_path, isolation=Isolation.HOST),
    )
    return result_row(
        plan, verdict=verdict, invalid_reason=reason, na_reason=None, status=TrajectoryStatus.COMPLETED,
        checks=(), usage=(), cost_source=CostSource.METERED, actual=(), unobserved=False,
        timings=(None, None, None, None), judge=(), same_family=False, web=WebIsolation.NONE, rel_dir=None,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("reason", "want"),
    [
        ("world internal error: boom", True),
        ("world registration failed: refused", True),
        ("meter unreachable: connection reset", True),
        ("boot_error", True),
        ("boot_error; start: TimeoutError: connection reset", True),
        ("boot_error; box: sidecar failed to start", True),
        ("boot_error; world registration failed: refused", True),
        ("execution incomplete: timeout; attribution requires semantic judging", True),
        # A site or provider outage the harness reported: waited out and retried.
        ("environment interruption: shop returned 503", True),
        # The campaign budget refused the start: another attempt would be refused the same way.
        ("boot_error; budget refused: $0.30 of $0.50 is committed", False),
        # A harness or box the run could not open would fail its probe again.
        ("boot_error; harness unavailable: not installed", False),
        ("docker box unavailable: no image", False),
        # The trial's own world state produced no evidence, so a re-run re-observes the same absence.
        ("mail.sent: 0 rows matched where to~priya (want 1)", False),
        # A judgment of the evidence the attempt produced, not a fault of the attempt.
        ("semantic judge rejected execution validity", False),
        # A second attempt would strand more remote resources than the first.
        ("harness teardown failed: TimeoutError: device still attached", False),
        ("product: teardown failed for the cloud device: 502", False),
    ],
)
def test_a_trial_is_retried_only_for_a_transient_execution_fault(tmp_path: Path, reason: str, want: bool) -> None:
    assert transient_invalid(_row(tmp_path, Verdict.INVALID, reason)) is want


def test_a_graded_verdict_is_never_retried(tmp_path: Path) -> None:
    assert not transient_invalid(_row(tmp_path, Verdict.PASS, None))
    assert not transient_invalid(_row(tmp_path, Verdict.FAIL, None))


def test_latest_results_keeps_the_retried_attempt_and_one_row_per_key(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    invalid = _row(tmp_path, Verdict.INVALID, "world internal error: boom")
    passed = invalid.model_copy(update={"verdict": Verdict.PASS, "invalid_reason": None})
    path.write_text(invalid.model_dump_json() + "\n" + passed.model_dump_json() + "\n")

    assert len(read_results(path)) == 2  # both attempts stay on disk as evidence
    (latest,) = latest_results(path)
    assert latest.verdict is Verdict.PASS


async def test_a_transient_invalid_trial_is_re_run_within_the_same_run(tmp_path: Path) -> None:
    task = discover([TASKS])[0]
    (plan,) = plan_trials(
        [task],
        [HarnessPlan(name="h", info=INFO, version="1", config_hash="c")],
        ["m/x"],
        RunOptions(out=tmp_path, isolation=Isolation.HOST),
    )
    transient = _row(tmp_path, Verdict.INVALID, "world internal error: boom")
    retried = transient.model_copy(update={"verdict": Verdict.PASS, "invalid_reason": None})

    class Flaky:
        def __init__(self) -> None:
            self.results = [transient, retried]
            self.calls: list[str] = []

        async def trial(self, p: runner.TrialPlan) -> runner.TrialRun:
            self.calls.append(p.key)
            return runner.TrialRun(self.results.pop(0), None)

    session = Flaky()
    opts_ = RunOptions(out=tmp_path, isolation=Isolation.HOST, quiet=True, retry_invalid=1)
    runs = {r.result.key: r for r in await runner.execute_all(session, [plan], opts_)}  # type: ignore[arg-type]
    await retry_transient_invalid(session, [plan], runs, opts_)  # type: ignore[arg-type]

    # One re-run for the transient fault; the permanent one is left as it stands.
    assert session.calls == [plan.key, plan.key]
    rows = read_results(tmp_path / "results.jsonl")
    assert [r.verdict for r in rows] == [Verdict.INVALID, Verdict.PASS]
    assert latest_results(tmp_path / "results.jsonl")[0].verdict is Verdict.PASS


_TEARDOWN_PROBE = """
import asyncio, sys
from parallax.runner import finish_uncancelled, run_to_teardown
async def cleanup():
    await asyncio.sleep(1)
    print("cleanup finished", flush=True)
async def main():
    try:
        print("running", flush=True)
        await asyncio.sleep(30)
    finally:
        await finish_uncancelled(cleanup())
try:
    run_to_teardown(main())
except KeyboardInterrupt:
    sys.exit(130)
"""


def test_a_second_sigint_does_not_cut_teardown_short() -> None:
    # `uv run` forwards its own copy of the terminal's Ctrl-C about 200 ms after the group's, so one
    # keypress arrives twice: plain `asyncio.run` raises on the second and strands the run's network.
    proc = subprocess.Popen([sys.executable, "-c", _TEARDOWN_PROBE], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "running"
    proc.send_signal(signal.SIGINT)
    time.sleep(0.3)
    proc.send_signal(signal.SIGINT)
    out, _ = proc.communicate(timeout=30)
    assert (proc.returncode, out.strip()) == (130, "cleanup finished")
