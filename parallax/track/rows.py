"""A run directory read back: its trials, the rows they group into, and what the run measured."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from parallax.models import (
    CostSource,
    HarnessKind,
    RunManifest,
    TrialResult,
    Verdict,
)
from parallax.stats import (
    cluster_bootstrap,
    is_valid,
    judge_identity,
    mean_of_means,
    pass_hat_k,
    quantile,
    row_label,
)


@dataclass(frozen=True)
class LoadedRun:
    path: Path
    manifest: RunManifest
    results: tuple[TrialResult, ...]  # the selected attempt of each trial: its key's last row
    attempts: tuple[TrialResult, ...] = ()  # every physical attempt, in the order they were written

    def physical_runs(self, key: str) -> tuple[TrialResult, ...]:
        """Every attempt of the trial `key`, first to last."""
        return tuple(r for r in self.attempts if r.key == key)

    @property
    def retries(self) -> int:
        """Attempts beyond each trial's first: retries within a run and re-runs on resume."""
        return len(self.attempts) - len(self.results)


def load_run(path: Path | str) -> LoadedRun:
    """Read manifest.json and results.jsonl. Every line is an attempt; the last for a key is the trial's selected one."""
    run = Path(path)
    manifest = RunManifest.model_validate_json((run / "manifest.json").read_text())
    attempts: list[TrialResult] = []
    by_key: dict[str, TrialResult] = {}
    for n, line in enumerate((run / "results.jsonl").read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            r = TrialResult.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(f"{run / 'results.jsonl'}:{n}: {exc}") from exc
        attempts.append(r)
        by_key[r.key] = r
    return LoadedRun(path=run, manifest=manifest, results=tuple(by_key.values()), attempts=tuple(attempts))


def is_validity_control(kind: HarnessKind, model: str | None) -> bool:
    """Oracle and nulls check the task, not an agent. A model-driven control (the agents-sdk baseline)
    is a scored reference row like any harness: validity controls never take a model."""
    return kind == HarnessKind.CONTROL and model is None


# identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """What a run measured, apart from its treatment: two runs compare only when these agree.

    The harness revision and the served models are the treatment a comparison measures. `provenance.json`
    records them, and no rule here requires them to match.
    """

    tasks: Mapping[str, str]  # task id -> content identity (the hash; the version in tracked runs)
    grading_policy: str
    judge_identity: str | None
    judge: tuple[str, ...]
    harness_versions: Mapping[str, tuple[str, ...]]  # row label -> the versions its trials ran
    config_hashes: Mapping[str, tuple[str, ...]]  # row label -> the harness configs its trials ran
    parallax_head: str | None


def identity_conflicts(base: Identity, cand: Identity) -> tuple[str, ...]:
    """Why two runs' verdicts cannot be paired; empty when they measured the same thing.

    A judge, grading policy, task, harness or Parallax change moves verdicts by itself, so a paired
    delta across one would report it as the treatment's effect. Pairing is over shared tasks and
    rows, so only those must agree.
    """
    out: list[str] = []
    if base.judge_identity != cand.judge_identity:
        out.append(f"judging identity differs ({base.judge_identity or 'none'} -> {cand.judge_identity or 'none'})")
    if base.grading_policy != cand.grading_policy:
        out.append(f"grading policy differs ({base.grading_policy} -> {cand.grading_policy})")
    if base.judge != cand.judge:
        out.append(f"judge differs ({', '.join(base.judge) or '-'} -> {', '.join(cand.judge) or '-'})")
    if base.parallax_head != cand.parallax_head:
        out.append(
            f"parallax head differs ({base.parallax_head or 'unrecorded'} -> {cand.parallax_head or 'unrecorded'})"
        )
    out += [
        f"task {task} differs ({base.tasks[task]} -> {cand.tasks[task]})"
        for task in sorted(base.tasks.keys() & cand.tasks.keys())
        if base.tasks[task] != cand.tasks[task]
    ]
    for what, before, after in (
        ("harness version", base.harness_versions, cand.harness_versions),
        ("harness config", base.config_hashes, cand.config_hashes),
    ):
        out += [
            f"{what} of {row} differs ({', '.join(before[row])} -> {', '.join(after[row])})"
            for row in sorted(before.keys() & after.keys())
            if before[row] != after[row]
        ]
    return tuple(out)


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RowScores:
    """One harness/model row's run-level numbers. None where no valid trial could produce one."""

    pass1: float | None
    pass1_ci: tuple[float, float] | None
    pass_k: float | None  # every one of k repeats passed, averaged over tasks with k valid repeats
    partial: float | None
    cost_per_task: float | None  # None when any valid trial's cost is unknown: a partial sum understates
    cost_source: CostSource
    agent_p50_ms: int | None
    n_valid: int
    n_invalid: int


def _cost_source(sources: set[CostSource]) -> CostSource:
    """The weakest source present labels the row: one harness-reported cost makes the total harness-reported."""
    if CostSource.NONE in sources:
        return CostSource.NONE
    if CostSource.HARNESS in sources:
        return CostSource.HARNESS
    return CostSource.METERED


def _pass_k_stat(k: int) -> Callable[[Sequence[Sequence[float]]], float]:
    def stat(clusters: Sequence[Sequence[float]]) -> float:
        return statistics.fmean(_task_pass_k(c, k) for c in clusters)

    return stat


def _task_pass_k(verdicts: Sequence[float], k: int) -> float:
    value = pass_hat_k(len(verdicts), round(sum(verdicts)), k)
    if value is None:
        raise ValueError("pass^k stat given a task with fewer than k repeats")
    return value


def row_scores(results: Sequence[TrialResult], k: int, seed: int) -> RowScores:
    judge_identity(results)  # raises: verdicts of different judges never pool into one score
    valid = [r for r in results if is_valid(r)]
    n_invalid = sum(r.verdict == Verdict.INVALID for r in results)
    if not valid:
        # Recorded with no scores: a harness that could run nothing is itself a finding.
        return RowScores(None, None, None, None, None, CostSource.NONE, None, 0, n_invalid)
    per_task: dict[str, list[float]] = defaultdict(list)
    partial: dict[str, list[float]] = defaultdict(list)
    for r in valid:
        per_task[r.task_id].append(1.0 if r.verdict == Verdict.PASS else 0.0)
        partial[r.task_id].append(r.score)
    enough = {t: v for t, v in per_task.items() if len(v) >= k}
    source = _cost_source({r.cost_source for r in valid})
    costs = [r.cost_usd for r in valid]
    known = None if source is CostSource.NONE or None in costs else sum(c for c in costs if c is not None)
    agent = [float(r.agent_ms) for r in valid if r.agent_ms is not None]
    return RowScores(
        pass1=mean_of_means(list(per_task.values())),
        pass1_ci=cluster_bootstrap(per_task, mean_of_means, seed=seed),
        pass_k=_pass_k_stat(k)(list(enough.values())) if enough else None,
        partial=mean_of_means(list(partial.values())),
        cost_per_task=known / len(valid) if known is not None else None,
        cost_source=source if known is not None else CostSource.NONE,
        agent_p50_ms=round(quantile(agent, 0.5)) if agent else None,
        n_valid=len(valid),
        n_invalid=n_invalid,
    )


def group_rows(results: Iterable[TrialResult]) -> dict[str, list[TrialResult]]:
    rows: dict[str, list[TrialResult]] = defaultdict(list)
    for r in results:
        rows[row_label(r.harness, r.model)].append(r)
    return dict(sorted(rows.items()))
