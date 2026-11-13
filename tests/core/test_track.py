"""What a run becomes in Langfuse, and when a comparison calls a regression.

Offline: the projection and the comparison are pure; the Langfuse calls are exercised
against a live instance, not mocked here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from parallax.models import Verdict
from parallax.track.compare import VERIFIED, Direction, RunRecord, TaskRecord, baseline_for, compare_rows
from parallax.track.projection import Outcome, item_id, judge_policy, outcome_of, project, project_trace
from parallax.track.rows import load_run

R1 = Path(__file__).parent / "fixtures" / "runs" / "r1"


def test_a_run_becomes_one_experiment_per_row_over_one_item_per_task_version() -> None:
    loaded = load_run(R1)
    tracked = project(loaded, dataset="ds")
    rows = {(r.harness, r.model) for r in loaded.results}
    assert {(run.harness, run.model) for run in tracked.runs} == rows
    versions = {(r.task_id, r.task_version) for r in loaded.results}
    assert {item.id for item in tracked.items} == {item_id("ds", t, v) for t, v in versions}
    for run in tracked.runs:
        assert {trace.item_id for trace in run.traces} <= {item.id for item in tracked.items}


def test_a_new_task_version_is_a_new_item_so_old_runs_never_pair_with_it() -> None:
    assert item_id("ds", "saas-a", 1) != item_id("ds", "saas-a", 2)


def test_only_ids_and_results_leave_the_machine() -> None:
    for run in project(load_run(R1)).runs:
        for trace in run.traces:
            assert set(trace.input.model_dump()) == {"task_id", "task_version"}


def test_free_text_execution_errors_are_hashed() -> None:
    loaded = load_run(R1)
    for run in project(loaded).runs:
        for trace in run.traces:
            for repeat in trace.output.repeats:
                assert repeat.invalid_reason is None


@pytest.mark.parametrize(
    ("verdicts", "outcome"),
    [
        ([1.0, 1.0], Outcome.PASS),
        ([0.0, 0.0], Outcome.FAIL),
        ([1.0, 0.0], Outcome.FLAKY),
        ([], Outcome.INVALID),
    ],
)
def test_outcome_summarises_the_valid_repeats(verdicts: list[float], outcome: Outcome) -> None:
    assert outcome_of(verdicts) is outcome


@pytest.mark.parametrize(
    "verdict,cost,spend,unknown",
    [
        (Verdict.INVALID, 0.20, 0.30, 0),
        (Verdict.UNGRADED, 0.20, 0.30, 0),
        (Verdict.UNGRADED, None, 0.10, 1),
        (Verdict.NA, None, 0.10, 1),
    ],
)
def test_unscored_repeats_preserve_spend_without_entering_quality(
    verdict: Verdict, cost: float | None, spend: float, unknown: int
) -> None:
    recorded = load_run(R1).results[0]
    first = recorded.model_copy(update={"repeat": 0, "verdict": Verdict.PASS, "score": 1.0, "cost_usd": 0.10})
    second = recorded.model_copy(update={"repeat": 1, "verdict": verdict, "score": 0.0, "cost_usd": cost})
    trace = project_trace("ds", "run", (first, second))
    scores = {score.name: score.value for score in trace.scores}
    assert trace.output.verdicts == [1.0]
    assert scores.get("unscored_repeats") == 1.0
    assert scores.get("observed_spend_usd") == pytest.approx(spend)
    assert scores.get("unknown_cost_repeats") == unknown
    assert scores["cost_usd"] == 0.10


def _run(
    name: str,
    at: int,
    tasks: dict[str, tuple[int, list[float]]],
    row: str = "h|m",
    branch: str = "main",
    verified: bool = True,
) -> RunRecord:
    records = {
        f"{task}@v{version}": TaskRecord(task, version, tuple(verdicts), None, None)
        for task, (version, verdicts) in tasks.items()
    }
    scores = {VERIFIED: 1.0} if verified else {}
    return RunRecord(name, name, datetime(2026, 10, at, tzinfo=UTC), {"row": row, "branch": branch}, scores, records)


PASSING = {f"t{i}": (1, [1.0, 1.0]) for i in range(20)}


@pytest.mark.parametrize(
    "scores,comparable",
    [
        ({"valid_trials": 9.0, "invalid_trials": 1.0}, True),
        ({"valid_trials": 8.0, "invalid_trials": 2.0}, False),
        ({"valid_trials": 0.0, "invalid_trials": 0.0}, False),
        ({}, False),
    ],
)
def test_environment_broken_runs_cannot_establish_a_regression_gate(scores: dict[str, float], comparable: bool) -> None:
    from dataclasses import replace

    from parallax.track.compare import validity_conflict

    base = replace(_run("base", 1, PASSING), scores={"valid_trials": 10.0, "invalid_trials": 0.0})
    candidate = replace(_run("candidate", 2, PASSING), scores=scores)
    assert (validity_conflict(base, candidate) is None) is comparable


@pytest.mark.parametrize(
    ("candidate", "direction", "now_failing"),
    [
        (PASSING, Direction.TIED, ()),
        (
            {**PASSING, **{f"t{i}": (1, [0.0, 0.0]) for i in range(10)}},
            Direction.REGRESSED,
            tuple(f"t{i}@v1" for i in range(10)),
        ),
        # One task failing out of twenty is listed but is not a significant drop.
        ({**PASSING, "t0": (1, [0.0, 0.0])}, Direction.TIED, ("t0@v1",)),
    ],
)
def test_a_row_regresses_only_when_the_paired_ci_is_below_zero(
    candidate: dict[str, tuple[int, list[float]]], direction: Direction, now_failing: tuple[str, ...]
) -> None:
    result = compare_rows(_run("base", 1, PASSING), _run("cand", 2, candidate))
    assert result is not None
    assert result.direction is direction
    assert result.flipped_to_fail == tuple(sorted(now_failing))


def test_a_regraded_task_is_reported_not_compared() -> None:
    base = _run("base", 1, {"a": (1, [1.0]), "b": (1, [1.0])})
    cand = _run("cand", 2, {"a": (2, [0.0]), "b": (1, [1.0])})
    result = compare_rows(base, cand)
    assert result is not None
    assert result.n_common == 1
    assert result.flipped_to_fail == ()
    assert result.version_changed == ("a v1->v2",)


def test_a_task_graded_in_one_run_only_is_reported_not_compared() -> None:
    base = _run("base", 1, {"a": (1, [1.0]), "b": (1, [])})
    cand = _run("cand", 2, {"a": (1, [1.0]), "b": (1, [1.0])})
    result = compare_rows(base, cand)
    assert result is not None
    assert result.n_common == 1
    assert result.graded_one_side == ("b@v1",)


def test_runs_sharing_no_task_version_do_not_compare() -> None:
    assert compare_rows(_run("base", 1, {"a": (1, [1.0])}), _run("cand", 2, {"a": (2, [1.0])})) is None


def test_the_baseline_is_the_newest_earlier_matching_run_of_the_same_row() -> None:
    old, mid, half_uploaded, other_row, feature, new = (
        _run("old", 1, PASSING),
        _run("mid", 2, PASSING),
        _run("half", 3, PASSING, verified=False),  # an upload killed partway: never a baseline
        _run("other", 3, PASSING, row="x|y"),
        _run("feature", 4, PASSING, branch="feature"),
        _run("new", 5, PASSING, branch="feature"),
    )
    runs = [old, mid, half_uploaded, other_row, feature, new]
    assert baseline_for(runs, new, {}) is feature
    assert baseline_for(runs, new, {"branch": "main"}) is mid
    assert baseline_for(runs, old, {}) is None


def test_a_named_run_is_the_baseline_even_when_a_newer_run_matches() -> None:
    old, mid, half_uploaded, other_row, new = (
        _run("old", 1, PASSING),
        _run("mid", 2, PASSING),
        _run("half", 3, PASSING, verified=False),
        _run("other", 3, PASSING, row="x|y"),
        _run("new", 5, PASSING),
    )
    runs = [old, mid, half_uploaded, other_row, new]
    # The oldest run can be named, which is the whole point: no need to rely on "the previous run".
    assert baseline_for(runs, new, {}, named=[old]) is old
    assert baseline_for(runs, new, {}, named=[old, mid]) is mid
    assert baseline_for(runs, new, {}, named=[half_uploaded]) is None  # still never a half-uploaded run
    assert baseline_for(runs, new, {}, named=[other_row]) is None  # nor another row's run


def test_a_named_baseline_must_also_match_the_metadata_filters() -> None:
    old, new = _run("old", 1, PASSING), _run("new", 5, PASSING)
    assert baseline_for([old, new], new, {"branch": "main"}, named=[old]) is old
    assert baseline_for([old, new], new, {"branch": "feature"}, named=[old]) is None


def test_the_judge_policy_identity_changes_with_the_judges() -> None:
    loaded = load_run(R1)
    trials = list(loaded.results)
    swapped = [t.model_copy(update={"judge": ("other/judge",)}) for t in trials]
    assert judge_policy(loaded.manifest, trials) == judge_policy(loaded.manifest, list(trials))
    assert judge_policy(loaded.manifest, trials) != judge_policy(loaded.manifest, swapped)
    regraded = loaded.manifest.model_copy(update={"grading_policy": "another-policy"})
    assert judge_policy(loaded.manifest, trials) != judge_policy(regraded, trials)


@pytest.mark.parametrize("value", [0, 0.0, 2, 2.5])
def test_numeric_score_transport_keeps_integer_and_zero_values(value):
    from unittest.mock import Mock

    from parallax.track.projection import Score
    from parallax.track.sink import _score

    client = Mock()
    _score(client, Score("cost", value, "NUMERIC"), {}, datetime(2026, 1, 1, tzinfo=UTC), dataset_run_id="run")
    client.create_score.assert_called_once()
    assert client.create_score.call_args.kwargs["value"] == value
    assert client.create_score.call_args.kwargs["data_type"] == "NUMERIC"


@pytest.mark.parametrize("run_name", ["hermes/sonnet", "nébula/ünïcode"])
def test_experiment_ids_match_the_sdk_runner(run_name: str) -> None:
    from langfuse import Langfuse
    from parallax.track.sink import experiment_id_for

    client = Langfuse(public_key="pk", secret_key="sk", base_url="http://localhost:1", tracing_enabled=False)
    expected = client._create_experiment_id(project_id="project", dataset_id="dataset", run_name=run_name)
    assert experiment_id_for("project", "dataset", run_name) == expected


def test_a_push_records_the_experiment_on_fixed_ids_without_dataset_run_items() -> None:
    import json
    from unittest.mock import Mock

    from langfuse import Langfuse
    from langfuse import LangfuseOtelSpanAttributes as Attr
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from parallax.track import sink

    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key="pk-push",  # the SDK shares resources per key; the id test's client has tracing off
        secret_key="sk",
        base_url="http://localhost:1",
        tracer_provider=TracerProvider(id_generator=sink.IDS),
        span_exporter=exporter,
    )
    client.api = Mock(spec=[])  # any Public API call, dataset_run_items included, fails the test
    client.create_score = Mock()
    tracked = project(load_run(R1), dataset="ds")
    run = tracked.runs[0]

    experiment_id = sink.experiment_id_for("project", "dataset", run.name)
    sink._push_run(client, tracked, "dataset", experiment_id, run, {}, lambda _row, _trace: b"evidence")
    client.flush()

    roots = [s for s in exporter.get_finished_spans() if s.attributes and Attr.EXPERIMENT_ID in s.attributes]
    assert len(roots) == len(run.traces)
    for span, trace in zip(roots, run.traces, strict=True):
        attributes = span.attributes or {}
        assert attributes[Attr.EXPERIMENT_ID] == experiment_id
        assert attributes[Attr.EXPERIMENT_NAME] == run.name
        assert attributes[Attr.EXPERIMENT_DATASET_ID] == "dataset"
        assert attributes[Attr.EXPERIMENT_ITEM_ID] == trace.item_id
        trace_id, span_id = sink.root_ids(experiment_id, trace.item_id)
        assert (format(span.context.trace_id, "032x"), format(span.context.span_id, "016x")) == (trace_id, span_id)
        assert attributes[Attr.EXPERIMENT_ITEM_ROOT_OBSERVATION_ID] == span_id
        assert json.loads(str(attributes[Attr.EXPERIMENT_METADATA]))["run_id"] == run.metadata["run_id"]
        assert json.loads(str(attributes[Attr.EXPERIMENT_ITEM_METADATA])) == {"task_id": trace.task_id}
    run_scores = [c.kwargs for c in client.create_score.call_args_list if c.kwargs.get("dataset_run_id")]
    assert {s["dataset_run_id"] for s in run_scores} == {experiment_id}
    assert len(run_scores) == len(run.scores)


def test_experiment_metadata_reads_the_same_from_per_key_and_json_attributes() -> None:
    from parallax.track.sink import metadata_value

    assert metadata_value("2026-01-01T00:00:00+00:00") == "2026-01-01T00:00:00+00:00"
    assert metadata_value(["a", "b"]) == '["a","b"]'
    assert (
        metadata_value(["Hermes Agent v0.22.6 · stable"]) == '["Hermes Agent v0.22.6 · stable"]'
    )  # as Langfuse holds it
    assert metadata_value(True) == "true"
    assert metadata_value(3) == "3"


@pytest.mark.parametrize("name", ["browser/suite-internal", "folder/space and %"])
def test_a_dataset_name_is_path_encoded_where_langfuse_takes_it_in_the_url(name: str) -> None:
    """`datasets.get` puts the name in the URL path, so a slash or space must be encoded or it names another route."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    from urllib.parse import quote

    from parallax.track import sink

    client = Mock()
    client.api.datasets.get.return_value = SimpleNamespace(id="dataset")
    client.api.experiments.list.return_value = SimpleNamespace(data=[], meta=SimpleNamespace(cursor=None))
    assert sink.list_runs(client, name) == []
    client.api.datasets.get.assert_called_once_with(dataset_name=quote(name, safe=""))
    sink._write_items(client, name, [])
    assert client.api.datasets.get.call_args.kwargs == {"dataset_name": quote(name, safe="")}
