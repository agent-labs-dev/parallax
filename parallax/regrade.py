"""Grade a finished run again under another judge, from what its trials recorded; no agent runs again.

A browser trial is graded from its trajectory (the harness's `trial_metadata`, its answers, status and steps) and
the reference date it ran under, all of which the trial directory keeps. Regrading writes a new run directory: a
copy of the source whose judged rows carry the new judge's checks, verdict and identity. A row no judge decided (a
boot error, an interrupted trial) is copied as it was.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import JsonValue

from parallax import provenance
from parallax.judge import JudgeConfig, JudgePanel
from parallax.models import RunManifest, Task, Trajectory, TrialResult
from parallax.runner import RESULTS, decide, judge_setup, read_results, score, tool_diagnostics
from parallax.semantic import grade_checks, policy_identity, record_grade
from parallax.track.upload import SEAL

JUDGMENTS = "judgments.jsonl"


class RegradeError(Exception):
    """The run cannot be regraded as asked."""


@dataclass(frozen=True)
class Regraded:
    out: Path
    regraded: int
    kept: int
    judge_cost_usd: float | None  # None when a judge call reported no price


async def regrade_trial(
    row: TrialResult, task: Task, trial: Path, panel: JudgePanel, config: JudgeConfig | None, client: httpx.AsyncClient
) -> tuple[TrialResult, float | None]:
    """Grade one trial from its directory, rewrite its judgments and result, and return the row and judge cost."""
    traj = Trajectory.model_validate_json((trial / "trajectory.json").read_text())
    notes = traj.extra.get("notes")
    execution: dict[str, JsonValue] = {
        "status": traj.status.value,
        "error": traj.error,
        "notes": list(notes) if isinstance(notes, list) else [],
        "tool_diagnostics": tool_diagnostics(traj.steps),
    }
    if (today := row.details.get("today")) is not None:
        execution["today"] = today
    product = traj.extra.get("harness_metadata")
    g = await grade_checks(
        task, None, panel, config, row.model, client=client, execution=execution,
        product=product if isinstance(product, dict) else None, answers=traj.answers,
    )  # fmt: skip
    (trial / JUDGMENTS).unlink()
    record_grade(trial / JUDGMENTS, task, g)
    verdict, reason = decide(
        None, traj.execution_invalid_reasons, traj.status, g.checks, execution_valid=g.execution_valid
    )
    new = row.model_copy(
        update={
            "verdict": verdict,
            "invalid_reason": reason,
            "checks": g.checks,
            "score": g.score if g.score is not None else score(g.checks) if g.checks else 0.0,
            "judge": panel.models,
            "judge_same_family": g.same_family,
            "judge_identity": g.judge_identity,
        }
    )
    (trial / "result.json").write_text(new.model_dump_json(indent=2), encoding="utf-8")
    costs = [j.cost() for j in g.judgments]
    unpriced = g.unpriced_calls + sum(n for _, n in costs)
    return new, None if unpriced else sum(known for known, _ in costs)


async def regrade(
    source: Path,
    out: Path,
    tasks: Sequence[Task],
    judge: Sequence[str],
    concurrency: int = 8,
    client: httpx.AsyncClient | None = None,
) -> Regraded:
    """Copy the run at `source` to `out` and grade every judged trial there again under `judge`."""
    if not (source / "manifest.json").is_file():
        raise RegradeError(f"{source}: not a run directory (no manifest.json)")
    if out.exists():
        raise RegradeError(f"{out} already exists")
    if out.resolve().is_relative_to(source.resolve()):
        raise RegradeError(f"{out} is inside the source run, which a regrade never changes")
    if concurrency < 1:
        raise RegradeError(f"concurrency must be at least 1, got {concurrency}")
    panel, config = judge_setup(judge)
    if panel is None:
        raise RegradeError("regrading needs a judge panel (--judge or PARALLAX_JUDGE_MODELS)")
    manifest = RunManifest.model_validate_json((source / "manifest.json").read_text())
    by_id = {t.id: t for t in tasks}
    # The task as it was run: a changed task would be graded against checks the agent never saw.
    for task_id, task_hash in manifest.tasks:
        if task_id not in by_id:
            raise RegradeError(f"task {task_id} is not in the selection; select the run's tasks")
        if by_id[task_id].hash != task_hash:
            raise RegradeError(f"task {task_id} changed since the run (hash {task_hash[:12]})")
        # A seeded task is graded from its world's evidence, which the trial directory does not hold in a form the
        # grader reads back; regrading it from nothing would turn every verdict INVALID.
        if by_id[task_id].seed is not None:
            raise RegradeError(f"task {task_id} has a world; regrade grades world-less (browser) tasks only")
    # Without the source's upload seal: the copy is a new run that has not been uploaded.
    shutil.copytree(source, out, ignore=shutil.ignore_patterns(SEAL))
    gate = asyncio.Semaphore(concurrency)

    async def one(row: TrialResult, client: httpx.AsyncClient) -> tuple[TrialResult, float | None, bool]:
        # Only a row a judge decided is decided again; its judgments journal is the record that one did.
        if row.dir is None or not (out / row.dir / JUDGMENTS).is_file():
            return row, 0.0, False
        async with gate:
            new, cost = await regrade_trial(row, by_id[row.task_id], out / row.dir, panel, config, client)
        return new, cost, True

    async with contextlib.AsyncExitStack() as stack:
        http = client if client is not None else await stack.enter_async_context(httpx.AsyncClient(timeout=120))
        done = await asyncio.gather(*(one(r, http) for r in read_results(out / RESULTS)))
    (out / RESULTS).write_text("".join(r.model_dump_json() + "\n" for r, _, _ in done), encoding="utf-8")
    identity = policy_identity([by_id[task_id] for task_id, _ in manifest.tasks], panel)
    regraded = manifest.model_copy(update={"run_id": out.name, "judge": panel.models, "grading_policy": identity})
    (out / "manifest.json").write_text(regraded.model_dump_json(indent=2), encoding="utf-8")
    if (out / provenance.PROVENANCE).is_file():
        prov = provenance.read(out)
        extra = {**prov.extra, "parallax.regraded_from": manifest.run_id}
        provenance.write(out, prov.model_copy(update={"judge_policy": identity, "extra": extra}))
    costs = [c for _, c, judged in done if judged]
    total = None if None in costs else sum(c for c in costs if c is not None)
    return Regraded(out=out, regraded=len(costs), kept=len(done) - len(costs), judge_cost_usd=total)
