"""`parallax compare` refuses a pair through `rows.identity_conflicts`, the one identity rule.

The run records are built from the real projection, so the metadata round-trips the way Langfuse
returns it: one string per key, list values JSON-encoded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from parallax.models import TrialResult
from parallax.stats import MixedJudgeIdentities
from parallax.track.compare import RunRecord, TaskRecord, compare_rows, identity_of
from parallax.track.projection import project
from parallax.track.rows import LoadedRun, identity_conflicts, load_run

R1 = Path(__file__).parent / "fixtures" / "runs" / "r1"
ROW = "alpha|m1"


def _tracked(*, head: str | None = "px1", policy: str = "p1", judge: tuple[str, ...] = (), trial=None) -> RunRecord:
    loaded = load_run(R1)
    loaded = LoadedRun(
        path=loaded.path,
        manifest=loaded.manifest.model_copy(update={"grading_policy": policy, "judge": judge}),
        results=tuple(trial(r) if trial else r for r in loaded.results),
    )
    spec = next(run for run in project(loaded, parallax_head=head).runs if run.row == ROW)
    metadata = {k: v if isinstance(v, str) else json.dumps(v) for k, v in spec.metadata.items()}
    tasks = {
        f"{r.task_id}@v{r.task_version}": TaskRecord(r.task_id, r.task_version, (), None, None)
        for r in loaded.results
        if r.harness == "alpha"
    }
    return RunRecord(spec.name, spec.name, datetime(2026, 10, 1, tzinfo=UTC), metadata, {}, tasks)


def _alpha(update: dict[str, object]):
    def rewrite(r: TrialResult) -> TrialResult:
        return r.model_copy(update=update) if r.harness == "alpha" else r

    return rewrite


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"trial": _alpha({"task_version": 2})}, "differs (v1 -> v2)"),
        ({"policy": "p2"}, "grading policy differs"),
        ({"judge": ("other/judge",)}, "judge differs"),
        ({"trial": _alpha({"harness_version": "2.0"})}, f"harness version of {ROW} differs"),
        ({"trial": _alpha({"config_hash": "cfg2"})}, f"harness config of {ROW} differs"),
        ({"head": "px2"}, "parallax head differs"),
        ({"head": None}, "parallax head differs (px1 -> unrecorded)"),
        ({"trial": _alpha({"judge_identity": "browser-evidence-v2:abc"})}, "judging identity differs (none ->"),
    ],
)
def test_a_tracked_pair_refuses_for_the_same_reasons_as_a_run_dir_pair(changes: dict[str, object], reason: str) -> None:
    conflicts = identity_conflicts(identity_of(_tracked()), identity_of(_tracked(**changes)))
    assert conflicts and all(reason in c for c in conflicts), conflicts


def test_a_tracked_pair_that_measured_the_same_thing_pairs() -> None:
    # The served models are the treatment: a candidate that served another model still pairs.
    served = _alpha({"actual_models": ("m2",)})
    assert identity_conflicts(identity_of(_tracked()), identity_of(_tracked(trial=served))) == ()


def test_runs_graded_under_different_judging_identities_are_never_compared() -> None:
    # No flag overrides this: the two sides' verdicts answer different questions.
    regraded = _tracked(trial=_alpha({"judge_identity": "browser-evidence-v2:abc"}))
    with pytest.raises(MixedJudgeIdentities, match="different judging identities"):
        compare_rows(_tracked(), regraded)
