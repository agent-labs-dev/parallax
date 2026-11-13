"""What a run becomes in Langfuse, computed without touching the network.

One dataset item per task *version*, so only trials graded the same way ever line up in a
comparison; a content edit that keeps the version updates the item in place, and Langfuse
keeps the earlier content as an item version. One Langfuse experiment per harness/model row of
the run, named after the run so a second push of the same run is recognised. One
experiment item per (row, task), holding every repeat.

The item output is the queryable summary: verdicts, failed checks, timing, usage and cost per
repeat. Everything else the trials wrote travels beside it as the item's evidence zip.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from parallax.models import (
    CheckKind,
    CostSource,
    FailureMode,
    LatencySplit,
    RunManifest,
    TrajectoryStatus,
    TrialResult,
    Usage,
    Verdict,
)
from parallax.stats import is_valid, judge_identity
from parallax.track.rows import LoadedRun, group_rows, is_validity_control, row_scores

DEFAULT_DATASET = "parallax"
# Run metadata Parallax writes itself, which compare and the baseline search read; `--meta` naming
# one is refused up front rather than silently losing to it.
RESERVED_META = frozenset({
    "run_id", "row", "harness", "model", "harness_versions", "config_hashes", "validity_control",
    "parallax_version", "judge", "judge_policy", "repeats", "isolation",
    "selection_sha", "tasks", "started_at", "parallax_head", "judge_identity",
})  # fmt: skip
META_VALUE_MAX = 200  # Langfuse truncates longer experiment metadata values


class Outcome(StrEnum):
    """A task's result across its repeats, as one categorical score."""

    PASS = "pass"  # every valid repeat passed
    FAIL = "fail"  # every valid repeat failed
    FLAKY = "flaky"  # valid repeats disagree
    INVALID = "invalid"  # no valid repeat: the eval, not the agent, decided this row


class ItemInput(BaseModel):
    """A dataset item's input, and an experiment item's: which task, graded which way."""

    task_id: str
    task_version: int


class CheckSummary(BaseModel):
    id: str
    kind: CheckKind
    mode: FailureMode
    gate: bool
    advisory: bool
    weight: float
    passed: bool | None


class RepeatDetail(BaseModel):
    repeat: int
    verdict: Verdict
    score: float
    cost_usd: float | None
    agent_ms: int | None
    wall_ms: int | None
    invalid_reason: str | None
    invalid_reason_sha256: str | None = None
    flags: list[str]
    task_sha256: str | None = None
    config_hash: str | None = None
    harness_version: str | None = None
    actual_models: tuple[str, ...] | None = None
    status: TrajectoryStatus | None = None
    cost_source: CostSource | None = None
    usage: tuple[Usage, ...] | None = None
    checks: tuple[CheckSummary, ...] | None = None
    boot_ms: int | None = None
    ttft_ms: int | None = None
    latency_split: LatencySplit | None = None


class ItemOutput(BaseModel):
    """An experiment item's output: one task's result for one row of a run."""

    outcome: Outcome
    verdicts: list[float]  # one per valid repeat; what `parallax compare` pairs
    failed_checks: list[str]
    cost_usd: float | None
    agent_ms: float | None
    harness_version: str
    config_hash: str
    actual_models: list[str]
    repeats: list[RepeatDetail]


@dataclass(frozen=True)
class ItemSpec:
    id: str
    input: ItemInput
    metadata: dict[str, object]


@dataclass(frozen=True)
class Score:
    name: str
    value: float | str
    data_type: Literal["NUMERIC", "CATEGORICAL"]
    comment: str | None = None


@dataclass(frozen=True)
class TraceSpec:
    item_id: str
    name: str
    task_id: str
    tags: tuple[str, ...]
    version: str  # "harness" or "harness/model"; dashboards break series down by it
    input: ItemInput
    output: ItemOutput
    scores: tuple[Score, ...]


@dataclass(frozen=True)
class RunSpec:
    name: str
    row: str
    harness: str
    model: str | None
    description: str
    metadata: dict[str, object]
    scores: tuple[Score, ...]
    traces: tuple[TraceSpec, ...]


@dataclass(frozen=True)
class TrackedRun:
    dataset: str
    run_id: str
    created_at: datetime
    items: tuple[ItemSpec, ...]
    runs: tuple[RunSpec, ...]


def item_id(dataset: str, task_id: str, task_version: int) -> str:
    """Langfuse item ids are unique per project, so the dataset is part of the id."""
    return f"{dataset}:{task_id}@v{task_version}"


def run_name(manifest: RunManifest, harness: str, model: str | None) -> str:
    return f"{manifest.created_at:%Y-%m-%d %H:%M} {harness}/{model or '-'} {manifest.run_id}"


def judge_policy(manifest: RunManifest, trials: Sequence[TrialResult]) -> str:
    """A short identity of what graded these trials, so a comparison only pairs runs graded alike.

    Covers the run's grading policy (judge policy version, Jev model and endpoint, fallback
    endpoint) and the judge models the trials actually used.
    """
    payload = {
        "grading_policy": manifest.grading_policy,
        "judges": sorted({judge for t in trials for judge in t.judge}),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def outcome_of(verdicts: Sequence[float]) -> Outcome:
    if not verdicts:
        return Outcome.INVALID
    if all(v == 1.0 for v in verdicts):
        return Outcome.PASS
    if all(v == 0.0 for v in verdicts):
        return Outcome.FAIL
    return Outcome.FLAKY


def _mean(values: Sequence[float] | Sequence[int]) -> float | None:
    return statistics.fmean(values) if values else None


def project_items(dataset: str, results: Sequence[TrialResult]) -> tuple[ItemSpec, ...]:
    items: dict[str, ItemSpec] = {}
    for r in sorted(results, key=lambda r: (r.task_id, r.task_version)):
        iid = item_id(dataset, r.task_id, r.task_version)
        items[iid] = ItemSpec(
            id=iid,
            input=ItemInput(task_id=r.task_id, task_version=r.task_version),
            metadata={
                "task_hash": r.task_hash,
                "area": r.area.value,
                "split": r.split.value,
                "maturity": r.maturity.value,
                "lite": r.lite,
            },
        )
    return tuple(items.values())


def project_trace(dataset: str, run_id: str, trials: Sequence[TrialResult]) -> TraceSpec:
    """One experiment item: the task's verdict per valid repeat, what failed, and each repeat in detail."""
    first = trials[0]
    iid = item_id(dataset, first.task_id, first.task_version)
    valid = [t for t in trials if is_valid(t)]
    verdicts = [1.0 if t.verdict is Verdict.PASS else 0.0 for t in valid]
    outcome = outcome_of(verdicts)
    costs = [t.cost_usd for t in valid if t.cost_usd is not None]
    agent_ms = [t.agent_ms for t in valid if t.agent_ms is not None]
    scores: list[Score] = [
        Score("outcome", outcome.value, "CATEGORICAL"),
        Score("unscored_repeats", float(len(trials) - len(valid)), "NUMERIC"),
        Score("unknown_cost_repeats", float(sum(t.cost_usd is None for t in trials)), "NUMERIC"),
    ]
    observed_costs = [t.cost_usd for t in trials if t.cost_usd is not None]
    if observed_costs:
        scores.append(Score("observed_spend_usd", sum(observed_costs), "NUMERIC", "all recorded trial costs"))
    if valid:
        scores.append(Score("pass_rate", statistics.fmean(verdicts), "NUMERIC", f"{len(valid)} valid repeats"))
        scores.append(Score("partial_credit", statistics.fmean(t.score for t in valid), "NUMERIC"))
    if (cost := _mean(costs)) is not None:
        scores.append(Score("cost_usd", cost, "NUMERIC", f"mean per trial, {first.cost_source.value}"))
    if (ms := _mean(agent_ms)) is not None:
        scores.append(Score("agent_seconds", ms / 1000, "NUMERIC", "mean agent time per trial"))
    repeats = [
        RepeatDetail(
            repeat=t.repeat,
            verdict=t.verdict,
            score=t.score,
            cost_usd=t.cost_usd,
            agent_ms=t.agent_ms,
            wall_ms=t.wall_ms,
            invalid_reason=None,
            invalid_reason_sha256=hashlib.sha256(t.invalid_reason.encode()).hexdigest() if t.invalid_reason else None,
            flags=sorted(f.value for f in t.flags),
            task_sha256=t.task_hash,
            config_hash=t.config_hash,
            harness_version=t.harness_version,
            actual_models=t.actual_models,
            status=t.status,
            cost_source=t.cost_source,
            usage=t.usage,
            checks=tuple(CheckSummary.model_validate(c.model_dump(exclude={"detail"})) for c in t.checks),
            boot_ms=t.boot_ms,
            ttft_ms=t.ttft_ms,
            latency_split=t.latency_split,
        )
        for t in sorted(trials, key=lambda t: t.repeat)
    ]
    return TraceSpec(
        item_id=iid,
        name=first.task_id,
        task_id=first.task_id,
        # Tags filter the trace and score views (and dashboards) to a run, harness, model or area.
        tags=(f"run:{run_id}", f"harness:{first.harness}", f"model:{first.model or '-'}", f"area:{first.area.value}"),
        version=first.harness if first.model is None else f"{first.harness}/{first.model}",
        input=ItemInput(task_id=first.task_id, task_version=first.task_version),
        output=ItemOutput(
            outcome=outcome,
            verdicts=verdicts,
            failed_checks=list(
                dict.fromkeys(c.id for t in trials for c in t.checks if c.passed is False and not c.advisory)
            ),
            cost_usd=_mean(costs),
            agent_ms=_mean(agent_ms),
            harness_version=first.harness_version,
            config_hash=first.config_hash,
            actual_models=sorted({m for t in trials for m in t.actual_models}),
            repeats=repeats,
        ),
        scores=tuple(scores),
    )


def _run_scores(trials: Sequence[TrialResult], k: int, seed: int) -> tuple[Score, ...]:
    row = row_scores(trials, k, seed)
    scores: list[Score] = []
    if row.pass1 is not None:
        ci = f"95% CI {row.pass1_ci[0]:.2f}-{row.pass1_ci[1]:.2f}" if row.pass1_ci is not None else None
        scores.append(Score("pass_at_1", row.pass1, "NUMERIC", ci))
    if row.pass_k is not None:
        # One name whatever k is, so runs with different repeat counts still chart as one series.
        scores.append(Score("pass_all_repeats", row.pass_k, "NUMERIC", f"pass^{k}: every one of {k} repeats passed"))
    if row.partial is not None:
        scores.append(Score("partial_credit", row.partial, "NUMERIC"))
    if row.cost_per_task is not None:
        scores.append(Score("cost_per_task_usd", row.cost_per_task, "NUMERIC", row.cost_source.value))
    if row.agent_p50_ms is not None:
        scores.append(Score("agent_p50_seconds", row.agent_p50_ms / 1000, "NUMERIC"))
    scores.append(Score("valid_trials", float(row.n_valid), "NUMERIC"))
    scores.append(Score("invalid_trials", float(row.n_invalid), "NUMERIC"))
    return tuple(scores)


def check_meta(meta: Mapping[str, str]) -> None:
    """Refuse caller metadata Langfuse would truncate or that would replace Parallax's own keys."""
    if clash := sorted(RESERVED_META & meta.keys()):
        raise ValueError(f"--meta {', '.join(clash)}: reserved for Parallax's own run metadata")
    if long := sorted(k for k, v in meta.items() if len(v) > META_VALUE_MAX):
        raise ValueError(f"--meta {', '.join(long)}: values are limited to {META_VALUE_MAX} characters")


def project(
    loaded: LoadedRun,
    *,
    dataset: str = DEFAULT_DATASET,
    note: str = "",
    extra: Mapping[str, str] | None = None,
    parallax_head: str | None = None,
) -> TrackedRun:
    """`parallax_head` comes from the run's `provenance.json`; a comparison requires it to match."""
    manifest = loaded.manifest
    check_meta(extra or {})
    if not loaded.results:
        raise ValueError(f"{loaded.path}: the run has no results to track")
    runs: list[RunSpec] = []
    for label, trials in group_rows(loaded.results).items():
        first = trials[0]
        by_task: dict[tuple[str, int], list[TrialResult]] = {}
        for t in trials:
            by_task.setdefault((t.task_id, t.task_version), []).append(t)
        traces = tuple(project_trace(dataset, manifest.run_id, by_task[key]) for key in sorted(by_task))
        runs.append(
            RunSpec(
                name=run_name(manifest, first.harness, first.model),
                row=label,
                harness=first.harness,
                model=first.model,
                description=note,
                # Langfuse stores a null value as "", so an absent value is left out rather than sent as None.
                metadata={
                    k: v
                    for k, v in {
                        **dict(extra or {}),  # first, so Parallax's own keys below always win
                        "run_id": manifest.run_id,
                        "row": label,
                        "harness": first.harness,
                        "model": first.model,
                        "harness_versions": sorted({t.harness_version for t in trials}),
                        "config_hashes": sorted({t.config_hash for t in trials}),
                        "validity_control": is_validity_control(first.harness_kind, first.model),
                        "parallax_version": manifest.parallax_version,
                        "judge": list(manifest.judge),
                        "judge_policy": judge_policy(manifest, trials),
                        # Absent unless a declared judging policy graded the row (`stats.judge_identity`).
                        "judge_identity": judge_identity(trials),
                        "repeats": manifest.repeats,
                        "isolation": manifest.isolation.value,
                        # Values are capped at META_VALUE_MAX characters; a selection can be far longer.
                        "selection_sha": hashlib.sha256(manifest.selection.encode()).hexdigest()[:12],
                        "tasks": len(by_task),
                        "started_at": manifest.created_at.isoformat(),
                        "parallax_head": parallax_head,
                    }.items()
                    if v is not None
                },
                scores=_run_scores(trials, manifest.repeats, manifest.bootstrap_seed),
                traces=traces,
            )
        )
    return TrackedRun(
        dataset=dataset,
        run_id=manifest.run_id,
        created_at=manifest.created_at,
        items=project_items(dataset, loaded.results),
        runs=tuple(runs),
    )
