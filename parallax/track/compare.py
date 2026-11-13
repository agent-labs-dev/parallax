"""Run-to-run regression checks over tracked runs, with paired bootstrap statistics.

Runs pair only when `rows.identity_conflicts` finds nothing, so neither a judge change nor a
re-graded task ever reads as a regression or an improvement; the row is reported as not compared
instead. A row regressed when the paired pass@1 delta's 95% CI lies wholly below zero;
single tasks that flipped are listed either way, because a flip in a small run is worth a
look even when the row is tied.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from parallax.stats import MixedJudgeIdentities, paired_delta
from parallax.track.rows import Identity


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    task_version: int
    verdicts: tuple[float, ...]
    cost_usd: float | None
    agent_ms: float | None


VERIFIED = "verified"  # the run-level score an upload writes once Langfuse is proven to hold the whole run


@dataclass(frozen=True)
class RunRecord:
    name: str
    id: str
    started_at: datetime
    metadata: dict[str, str]
    scores: dict[str, float]
    tasks: dict[str, TaskRecord]  # keyed by "<task>@v<version>"; empty until loaded

    @property
    def row(self) -> str:
        return self.metadata.get("row", "")


class Direction(StrEnum):
    REGRESSED = "regressed"
    IMPROVED = "improved"
    TIED = "tied"


@dataclass(frozen=True)
class RowComparison:
    row: str
    base: str
    candidate: str
    n_common: int
    delta_pass1: float
    ci: tuple[float, float]
    direction: Direction
    flipped_to_fail: tuple[str, ...]
    flipped_to_pass: tuple[str, ...]
    version_changed: tuple[str, ...]  # same task, different version: not compared
    graded_one_side: tuple[str, ...]  # in both runs, graded in only one: not compared
    cost_delta_usd: float | None
    agent_delta_seconds: float | None


def _verdicts(tasks: dict[str, TaskRecord]) -> dict[str, list[float]]:
    return {key: list(t.verdicts) for key, t in tasks.items() if t.verdicts}


def _mean_delta(
    base: dict[str, TaskRecord], cand: dict[str, TaskRecord], common: Sequence[str], field: str
) -> float | None:
    pairs = [
        (getattr(cand[k], field), getattr(base[k], field))
        for k in common
        if getattr(cand[k], field) is not None and getattr(base[k], field) is not None
    ]
    return statistics.fmean(c - b for c, b in pairs) if pairs else None


def compare_rows(base: RunRecord, cand: RunRecord, *, seed: int = 0) -> RowComparison | None:
    """None when the two runs share no comparable task. Raises `MixedJudgeIdentities` for runs graded under different
    judging identities: their verdicts answer different questions, and no flag pairs them."""
    before, after = base.metadata.get("judge_identity"), cand.metadata.get("judge_identity")
    if before != after:
        raise MixedJudgeIdentities(
            f"{base.name} and {cand.name} were graded under different judging identities "
            f"({before or 'none'} -> {after or 'none'}); grade both under one policy to compare them"
        )
    a, b = _verdicts(cand.tasks), _verdicts(base.tasks)
    common = sorted(set(a) & set(b))
    if not common:
        return None
    delta, ci = paired_delta(a, b, seed=seed)
    passing_now = {k for k in common if statistics.fmean(a[k]) >= 0.5}
    passing_before = {k for k in common if statistics.fmean(b[k]) >= 0.5}
    base_ids = {t.task_id: key for key, t in base.tasks.items()}
    version_changed = tuple(
        sorted(
            f"{t.task_id} v{base.tasks[base_ids[t.task_id]].task_version}->v{t.task_version}"
            for key, t in cand.tasks.items()
            if t.task_id in base_ids and base_ids[t.task_id] != key
        )
    )
    agent = _mean_delta(base.tasks, cand.tasks, common, "agent_ms")
    return RowComparison(
        row=cand.row,
        base=base.name,
        candidate=cand.name,
        n_common=len(common),
        delta_pass1=delta,
        ci=ci,
        direction=Direction.REGRESSED if ci[1] < 0 else Direction.IMPROVED if ci[0] > 0 else Direction.TIED,
        flipped_to_fail=tuple(sorted(passing_before - passing_now)),
        flipped_to_pass=tuple(sorted(passing_now - passing_before)),
        version_changed=version_changed,
        graded_one_side=tuple(sorted((set(base.tasks) & set(cand.tasks)) - set(common))),
        cost_delta_usd=_mean_delta(base.tasks, cand.tasks, common, "cost_usd"),
        agent_delta_seconds=agent / 1000 if agent is not None else None,
    )


def validity_conflict(base: RunRecord, cand: RunRecord) -> str | None:
    for run in (base, cand):
        valid, invalid = run.scores.get("valid_trials"), run.scores.get("invalid_trials")
        if valid is None or invalid is None or valid + invalid <= 0:
            return f"{run.name} does not establish execution validity"
        if invalid / (valid + invalid) > 0.10:
            return f"{run.name} exceeds the 10% invalid-share gate"
    return None


def identity_of(run: RunRecord) -> Identity:
    """The run's `rows.Identity`, from its experiment metadata and loaded tasks (`load_tasks`).

    A tracked task is identified by its version, the key its experiment items carry.
    """
    meta = run.metadata
    return Identity(
        tasks={t.task_id: f"v{t.task_version}" for t in run.tasks.values()},
        grading_policy=meta["judge_policy"],
        judge_identity=meta.get("judge_identity"),
        judge=_listed(meta.get("judge")),
        harness_versions={run.row: _listed(meta.get("harness_versions"))},
        config_hashes={run.row: _listed(meta.get("config_hashes"))},
        parallax_head=meta.get("parallax_head"),
    )


def _listed(value: str | None) -> tuple[str, ...]:
    """A list-valued metadata key, which travels JSON-encoded."""
    return tuple(json.loads(value)) if value else ()


def baseline_for(
    runs: Sequence[RunRecord],
    run: RunRecord,
    where: Mapping[str, str],
    *,
    named: Sequence[RunRecord] = (),
) -> RunRecord | None:
    """The newest earlier run of this row matching `where`, VERIFIED so a half-uploaded one is never the baseline.

    With `named`, the baseline must be one of those runs: a caller that named a run id or experiment name
    gets that run, not the newest one that happens to match the metadata filters.
    """
    if named:
        pool = [r for r in named if r.row == run.row and r.id != run.id and VERIFIED in r.scores]
        pool = [r for r in pool if all(r.metadata.get(k) == v for k, v in where.items())]
        return max(pool, key=lambda r: r.started_at) if pool else None
    earlier = [r for r in runs if r.row == run.row and r.started_at < run.started_at and VERIFIED in r.scores]
    earlier = [r for r in earlier if all(r.metadata.get(k) == v for k, v in where.items())]
    return max(earlier, key=lambda r: r.started_at) if earlier else None


def _signed(value: float | None, unit: str, digits: int) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}{unit}"


def render(comparisons: Sequence[RowComparison]) -> str:
    lines = [
        "| row | base -> candidate | tasks | delta pass@1 (95% CI) | verdict | cost/task | agent time |",
        "|---|---|---:|---|---|---:|---:|",
    ]
    for c in comparisons:
        lines.append(
            f"| {c.row.replace('|', '/')} | {c.base} -> {c.candidate} | {c.n_common} | "
            f"{c.delta_pass1:+.1%} ({c.ci[0]:+.1%} to {c.ci[1]:+.1%}) | {c.direction.value} | "
            f"{_signed(c.cost_delta_usd, ' USD', 4)} | {_signed(c.agent_delta_seconds, ' s', 1)} |"
        )
    for c in comparisons:
        row = c.row.replace("|", "/")
        if c.flipped_to_fail:
            lines.append(f"\n{row}: now failing: {', '.join(c.flipped_to_fail)}")
        if c.flipped_to_pass:
            lines.append(f"\n{row}: now passing: {', '.join(c.flipped_to_pass)}")
        if c.version_changed:
            lines.append(f"\n{row}: not compared, task version changed: {', '.join(c.version_changed)}")
        if c.graded_one_side:
            lines.append(f"\n{row}: not compared, graded in one run only: {', '.join(c.graded_one_side)}")
    return "\n".join(lines)
