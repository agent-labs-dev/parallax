"""Statistics for comparing runs: pass^k, task-clustered bootstrap, paired deltas.

Stdlib only. Every resampling routine takes an explicit seed so a comparison is reproducible.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Mapping, Sequence

from parallax.models import TrialResult, Verdict


def pass_hat_k(n: int, c: int, k: int) -> float | None:
    """Unbiased pass^k: the chance that k repeats drawn without replacement from n all pass (C(c,k)/C(n,k))."""
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    if not 0 <= c <= n:
        raise ValueError(f"need 0 <= c <= n, got c={c} n={n}")
    if n < k:
        return None
    return math.comb(c, k) / math.comb(n, k)


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile (numpy's default method), so p50/p90 match what readers expect."""
    if not values:
        raise ValueError("quantile of an empty sequence")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def mean_of_means(clusters: Sequence[Sequence[float]]) -> float:
    """pass@1: each task weighs the same however many valid repeats it has."""
    return statistics.fmean(statistics.fmean(c) for c in clusters)


def cluster_bootstrap(
    per_task: Mapping[str, Sequence[float]],
    stat: Callable[[Sequence[Sequence[float]]], float],
    b: int = 10_000,
    seed: int = 0,
) -> tuple[float, float]:
    """95% percentile CI of `stat`, resampling whole tasks (clusters) so repeats of one task stay together."""
    clusters = [per_task[t] for t in sorted(per_task)]
    if not clusters or any(len(c) == 0 for c in clusters):
        raise ValueError("cluster_bootstrap needs at least one task, each with at least one value")
    rng = random.Random(seed)
    idx = range(len(clusters))
    draws = [stat([clusters[i] for i in rng.choices(idx, k=len(clusters))]) for _ in range(b)]
    return quantile(draws, 0.025), quantile(draws, 0.975)


def paired_delta(
    a: Mapping[str, Sequence[float]], b: Mapping[str, Sequence[float]], seed: int = 0
) -> tuple[float, tuple[float, float]]:
    """Mean per-task difference a - b on the task intersection, with a task-clustered bootstrap CI."""
    common = sorted(set(a) & set(b))
    diffs = {t: [statistics.fmean(a[t]) - statistics.fmean(b[t])] for t in common if a[t] and b[t]}
    if not diffs:
        raise ValueError("paired_delta needs at least one task with values on both sides")
    delta = statistics.fmean(d[0] for d in diffs.values())
    return delta, cluster_bootstrap(diffs, lambda cs: statistics.fmean(c[0] for c in cs), seed=seed)


def is_valid(r: TrialResult) -> bool:
    """PASS/FAIL trials carry signal; NA was never run and INVALID is the eval's fault."""
    match r.verdict:
        case Verdict.PASS | Verdict.FAIL:
            return True
        case Verdict.NA | Verdict.INVALID | Verdict.UNGRADED:
            return False


def row_label(harness: str, model: str | None) -> str:
    return f"{harness}|{model if model is not None else '-'}"


class MixedJudgeIdentities(ValueError):
    """Rows graded under different judging identities were pooled: their verdicts answer different questions."""


def judge_identity(results: Sequence[TrialResult]) -> str | None:
    """The one judging identity `results` were graded under (None: rows graded by a policy that declares none).

    Pooling rows across identities would average verdicts of different judges, so it is refused: re-grade them under
    one policy, or report them apart. A row without an identity pools only with rows without one.
    """
    # An INVALID row with no identity was refused before any policy graded it, so it names no judge.
    graded = (
        r for r in results if r.verdict is not Verdict.NA and (r.judge_identity or r.verdict is not Verdict.INVALID)
    )
    found = sorted({r.judge_identity or "" for r in graded})
    if len(found) > 1:
        named = ", ".join(i or "(none)" for i in found)
        raise MixedJudgeIdentities(f"rows graded under {len(found)} judging identities cannot pool: {named}")
    return (found[0] or None) if found else None
