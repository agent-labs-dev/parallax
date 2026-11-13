"""Controls replay oracle/null evidence and mutants through Jev with LLM fallback.

These are semantic calibration runs and require configured judges. Offline tests only
verify replay, evidence transport and verdict propagation; they do not certify quality.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from parallax import __version__
from parallax.box import seed_dir, trusted_verify
from parallax.harnesses.base import load_harness
from parallax.judge import JudgeConfig, JudgePanel
from parallax.models import (
    ApprovalAnswer,
    Check,
    CheckResult,
    CommandCheck,
    Evidence,
    Isolation,
    Model,
    Mutation,
    MutationExpect,
    ScriptedCall,
    Task,
    Verdict,
    WorldCall,
    WorldEvidence,
)
from parallax.runner import (
    HarnessPlan,
    Session,
    TrialPlan,
    TrialRun,
    harness_config_hash,
    judge_setup,
    run_to_teardown,
    services,
)
from parallax.semantic import grade_checks, record_grade
from parallax.task import lint
from parallax.world.core import WorldError, diff_states, patch_state

ORACLE, NULL_CLAIM, NULL_REFUSE = "oracle", "null-claim", "null-refuse"
CONTROL_HARNESSES = (ORACLE, NULL_CLAIM, NULL_REFUSE)
CHECK_CONCURRENCY = 4  # tasks checked at once


class MutantOutcome(Model):
    id: str
    expect: MutationExpect
    ok: bool  # killed, for expect=fail; passed every gate, for expect=pass
    detail: str


class TaskControls(Model):
    task_id: str
    lint: tuple[str, ...]
    oracle: Verdict
    null_claim: Verdict
    null_refuse: Verdict
    problems: tuple[str, ...]  # human-readable reasons behind any non-ok verdict above
    mutants: tuple[MutantOutcome, ...]
    collateral_missing: bool

    @property
    def ok(self) -> bool:
        return (
            not self.lint
            and self.oracle is Verdict.PASS
            and self.null_claim is Verdict.FAIL
            and self.null_refuse is Verdict.FAIL
            and all(m.ok for m in self.mutants)
            and not self.collateral_missing
        )


class ControlsReport(Model):
    tasks: tuple[TaskControls, ...]

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.tasks)


# ---------------------------------------------------------------------------
# mutants
# ---------------------------------------------------------------------------


def scripted(calls: Sequence[WorldCall], extra: Sequence[ScriptedCall], ev: WorldEvidence) -> tuple[WorldCall, ...]:
    out = list(calls)
    for call in extra:
        session = next((c.session for c in reversed(out) if c.turn <= call.turn), 1)
        out.append(
            WorldCall(
                seq=len(out) + 1,
                turn=call.turn,
                session=session,
                at=ev.seed_now,
                tool=call.tool,
                service=call.tool.partition("_")[0],
                args=call.args,
                ok=call.ok,
                result={},
                mutated=call.mutated,
            )  # fmt: skip
        )
    return tuple(out)


def mutate_world(base: WorldEvidence, m: Mutation) -> WorldEvidence:
    from_seed = m.base == "seed"
    final = base.seed_state if from_seed else base.final
    if m.patch is not None:
        final = patch_state(final, m.patch)
    calls = scripted(() if from_seed else base.calls, m.extra_calls, base)
    return base.model_copy(update={"final": final, "diff": diff_states(base.seed_state, final), "calls": calls})


def mutate_files(task: Task, oracle_ws: Path | None, m: Mutation, into: Path) -> Path:
    """A fresh copy of the workspace the mutant starts from, with its file edits applied."""
    source = seed_dir(task) if m.base == "seed" else oracle_ws
    ws = into / "workspace"
    if source is not None and source.is_dir():
        shutil.copytree(source, ws, symlinks=True)
    else:
        ws.mkdir(parents=True)
    apply_files(ws, m.files)
    return ws


def mutate_turn_files(oracle: Evidence, m: Mutation, into: Path) -> dict[int, Path]:
    """The oracle's turn snapshots, with the mutant's per-turn edits applied to fresh copies."""
    out = dict(oracle.turn_workspaces)
    for turn, edits in m.turn_files.items():
        base = out.get(turn)
        if base is None:
            raise ValueError(f"mutation {m.id}: the oracle has no snapshot after turn {turn}")
        ws = into / "turns" / str(turn)
        shutil.copytree(base, ws, symlinks=True)
        apply_files(ws, edits)
        out[turn] = ws
    return out


def apply_files(ws: Path, edits: Mapping[str, str | None]) -> None:
    for rel, content in edits.items():
        target = ws / rel
        if content is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def mutate_answers(answers: tuple[str, ...], m: Mutation) -> tuple[str, ...]:
    out = list(answers)
    for turn, text in m.answers.items():
        if turn < 1:
            raise ValueError(f"mutation {m.id}: answer turn {turn} must be 1-based")
        out += [""] * (turn - len(out))
        out[turn - 1] = text
    return tuple(out)


def mutate_finals(finals: tuple[str, ...], m: Mutation) -> tuple[str, ...]:
    """A replacement answer is one message unless the mutant names its closing message."""
    out = list(mutate_answers(finals, m))
    for turn, text in m.finals.items():
        out[turn - 1] = text.strip()
    return tuple(out)


def mutate_approvals(task: Task, approvals: tuple[ApprovalAnswer, ...], m: Mutation) -> tuple[ApprovalAnswer, ...]:
    """A mutant's turns raise exactly the requests it lists, each answered with that step's decision;
    `base: seed` starts from none."""
    kept = [a for a in (() if m.base == "seed" else approvals) if a.turn not in m.approvals]
    for turn, requests in m.approvals.items():
        decision = task.turns[turn - 1].approval if 1 <= turn <= len(task.turns) else None
        if decision is None:
            raise ValueError(f"mutation {m.id}: turn {turn} scripts no approval decision")
        kept += [ApprovalAnswer(turn=turn, request=request, decision=decision) for request in requests]
    return tuple(sorted(kept, key=lambda a: a.turn))


def graded_by(task: Task, m: Mutation) -> list[Check] | str:
    """The checks a mutant is judged on, or why it names checks that do not exist."""
    checks = {c.id: c for c in task.checks.checks}
    match m.expect:
        case MutationExpect.FAIL:
            unknown = sorted(set(m.must_fail) - set(checks))
            return f"must_fail names unknown checks {unknown}" if unknown else [checks[i] for i in m.must_fail]
        case MutationExpect.PASS:
            # A correct answer must clear the whole semantic gate, not a hand-picked subset.
            return [c for c in checks.values() if c.gate]


async def kill(
    task: Task,
    oracle: Evidence,
    m: Mutation,
    isolation: Isolation,
    panel: JudgePanel | None = None,
    config: JudgeConfig | None = None,
    journal: Path | None = None,
) -> MutantOutcome:
    named = graded_by(task, m)
    if isinstance(named, str):
        return MutantOutcome(id=m.id, expect=m.expect, ok=False, detail=named)
    try:
        world = mutate_world(oracle.world, m)
    except WorldError as exc:
        detail = f"patch does not apply to the oracle state: {exc}"
        return MutantOutcome(id=m.id, expect=m.expect, ok=False, detail=detail)
    with tempfile.TemporaryDirectory(prefix=f"px-mutant-{m.id}-") as tmp:
        ws = mutate_files(task, oracle.workspace, m, Path(tmp))
        try:
            turn_ws = mutate_turn_files(oracle, m, Path(tmp))
        except ValueError as exc:
            return MutantOutcome(id=m.id, expect=m.expect, ok=False, detail=str(exc))
        try:
            approvals = mutate_approvals(task, oracle.approvals, m)
        except ValueError as exc:
            return MutantOutcome(id=m.id, expect=m.expect, ok=False, detail=str(exc))
        verify = dict(oracle.verify)
        commands = [c for c in named if isinstance(c, CommandCheck)]
        if commands and (m.files or m.base == "seed"):
            verify |= await trusted_verify(task, ws, commands, isolation=isolation)
        ev = oracle.model_copy(
            update={
                "world": world,
                "answers": mutate_answers(oracle.answers, m),
                "finals": mutate_finals(oracle.finals, m),
                "workspace": ws,
                "turn_workspaces": turn_ws,
                "verify": verify,
                "approvals": approvals,
            }
        )
        graded = await grade_checks(task, ev, panel, config, None)
        if journal is not None:
            record_grade(journal, task, graded)
        ids = {c.id for c in named}
        results = [c for c in graded.checks if c.id in ids]
    return outcome(m, results)


def outcome(m: Mutation, results: Sequence[CheckResult]) -> MutantOutcome:
    failed = [f"{r.id}: {r.detail}" for r in results if r.passed is False]
    missing = [f"{r.id} ({r.detail})" for r in results if r.passed is None]
    match m.expect:
        case MutationExpect.FAIL:
            if failed:
                killers = ", ".join(r.id for r in results if r.passed is False)
                return MutantOutcome(id=m.id, expect=m.expect, ok=True, detail=f"killed by {killers}")
            survived = "; ".join(f"{r.id}: {r.detail}" for r in results if r.passed)
            detail = f"missing evidence: {'; '.join(missing)}" if missing else f"survived: {survived}"
        case MutationExpect.PASS:
            # Missing evidence is not a pass: a must-pass answer has to be graded, not skipped.
            if not failed and not missing:
                return MutantOutcome(id=m.id, expect=m.expect, ok=True, detail=f"passed {len(results)} gates")
            detail = "; ".join([*(f"false fail {f}" for f in failed), *(f"missing evidence: {x}" for x in missing)])
    return MutantOutcome(id=m.id, expect=m.expect, ok=False, detail=detail)


# ---------------------------------------------------------------------------
# write detection
# ---------------------------------------------------------------------------


def files_of(root: Path | None) -> dict[str, bytes]:
    if root is None or not root.is_dir():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


def writes(task: Task, oracle: Evidence) -> bool:
    """The oracle changed the world or the workspace, so a careless agent could change too much."""
    return bool(oracle.world.diff.tables) or files_of(oracle.workspace) != files_of(seed_dir(task))


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------


def verdict_problem(control: str, run: TrialRun, want: Verdict) -> list[str]:
    r = run.result
    if r.verdict is want:
        return []
    failed = [f"{c.id}: {c.detail}" for c in r.checks if c.passed is not True and not c.advisory]
    reason = r.invalid_reason or r.na_reason or "; ".join(failed[:4]) or "no failing check"
    return [f"{control} {r.verdict.value} (want {want.value}): {reason}"]


async def check_task(session: Session, task: Task, plans: dict[str, HarnessPlan], isolation: Isolation) -> TaskControls:
    def trial(name: str) -> TrialPlan:
        return TrialPlan(task=task, harness=plans[name], model=None, repeat=0, key=f"check-{task.id}-{name}")

    oracle, claim, refuse = await asyncio.gather(*(session.trial(trial(n)) for n in CONTROL_HARNESSES))
    problems = [
        *verdict_problem(ORACLE, oracle, Verdict.PASS),
        *verdict_problem(NULL_CLAIM, claim, Verdict.FAIL),
        *verdict_problem(NULL_REFUSE, refuse, Verdict.FAIL),
    ]
    mutants: list[MutantOutcome] = []
    collateral_missing = False
    ev = oracle.evidence
    if ev is not None and oracle.result.verdict is Verdict.PASS:
        mutants = [
            await kill(
                task,
                ev,
                m,
                isolation,
                session.panel,
                session.judge_config,
                session.run_dir / task.id / "mutants" / m.id / "judgments.jsonl",
            )
            for m in task.mutations.mutations
        ]
        # Only a must-fail mutant proves a collateral check bites; a must-pass one proves the opposite.
        collateral = [m for m in task.mutations.mutations if m.collateral and m.expect is MutationExpect.FAIL]
        collateral_missing = writes(task, ev) and not collateral
    elif task.mutations.mutations:
        problems.append("mutants not evaluated: they apply to a passing oracle's evidence")
    return TaskControls(
        task_id=task.id, lint=tuple(lint(task)), oracle=oracle.result.verdict, null_claim=claim.result.verdict,
        null_refuse=refuse.result.verdict, problems=tuple(problems), mutants=tuple(mutants),
        collateral_missing=collateral_missing,
    )  # fmt: skip


async def check_async(tasks: Sequence[Task], isolation: Isolation) -> ControlsReport:
    if not tasks:
        raise ValueError("nothing to check: the selection matched no tasks")
    # Controls are never uploaded, so nothing of them outlives the check; problems are reported inline.
    with tempfile.TemporaryDirectory(prefix="parallax-check-") as scratch:
        out = Path(scratch)
        async with services(isolation, None, False, f"check-{out.name[-8:].lower()}") as svc:
            plans = {name: control_plan(name) for name in CONTROL_HARNESSES}
            panel, config = judge_setup(())
            session = Session(svc, out, panel, config)
            gate = asyncio.Semaphore(CHECK_CONCURRENCY)

            async def one(task: Task) -> TaskControls:
                async with gate:
                    return await check_task(session, task, plans, isolation)

            results = await asyncio.gather(*(one(t) for t in tasks))
    return ControlsReport(tasks=tuple(results))


def control_plan(name: str) -> HarnessPlan:
    harness = load_harness(name)
    return HarnessPlan(name=name, info=harness.info, version=__version__, config_hash=harness_config_hash(harness))  # fmt: skip


def check(tasks: Sequence[Task], isolation: Isolation) -> ControlsReport:
    return run_to_teardown(check_async(tasks, isolation))


def render(report: ControlsReport) -> str:
    lines: list[str] = []
    for t in report.tasks:
        kill = [m for m in t.mutants if m.expect is MutationExpect.FAIL]
        keep = [m for m in t.mutants if m.expect is MutationExpect.PASS]
        mark = "ok  " if t.ok else "FAIL"
        must_pass = f" must-pass={sum(m.ok for m in keep)}/{len(keep)} passed" if keep else ""
        lines.append(
            f"{mark} {t.task_id}: oracle={t.oracle.value} null-claim={t.null_claim.value} "
            f"null-refuse={t.null_refuse.value} mutants={sum(m.ok for m in kill)}/{len(kill)} killed{must_pass}"
        )
        lines += [f"     lint: {p}" for p in t.lint]
        lines += [f"     {p}" for p in t.problems]
        lines += [f"     mutant {m.id} (expect {m.expect.value}): {m.detail}" for m in t.mutants if not m.ok]
        if t.collateral_missing:
            lines.append("     the oracle writes, but no mutant is marked collateral: true")
    n_ok = sum(t.ok for t in report.tasks)
    lines.append(f"{n_ok}/{len(report.tasks)} tasks pass their controls")
    return "\n".join(lines)
