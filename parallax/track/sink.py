"""Langfuse, the only store of Parallax results: a project guard, idempotent pushes and run reads.

Credentials come from the SDK's own variables (`LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`). Writes additionally need `PARALLAX_LANGFUSE_PROJECT`, the id or
name of the project eval data belongs in: keys for any other project (a production
tracing project, say) fail closed before anything is written.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from langfuse import Langfuse, LangfuseMedia, propagate_attributes
from langfuse import LangfuseOtelSpanAttributes as Attr
from langfuse.api import (
    ConfigCategory,
    ExperimentItem,
    MediaContentType,
    NotFoundError,
    ScoreConfigDataType,
)
from langfuse.api.core import ApiError
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from pydantic import ValidationError

from parallax.track.compare import VERIFIED, RunRecord, TaskRecord
from parallax.track.projection import (
    ItemInput,
    ItemOutput,
    ItemSpec,
    Outcome,
    RunSpec,
    Score,
    TraceSpec,
    TrackedRun,
)

logger = logging.getLogger(__name__)

PROJECT_ENV = "PARALLAX_LANGFUSE_PROJECT"
# The item output field holding the zip of the task's trial directories.
EVIDENCE = "evidence"
EVIDENCE_TYPE = MediaContentType.APPLICATION_ZIP
PAGE = 100
# The environment the SDK gives experiments; the Langfuse UI files them under it.
EXPERIMENT_ENVIRONMENT = "sdk-experiment"
# The experiments API needs a lower time bound; every Parallax run is later than this.
EPOCH = datetime(2024, 1, 1, tzinfo=UTC)
# The experiment metadata compare reads; an experiment lacking any of it is not a Parallax run.
REQUIRED_META = ("run_id", "row", "judge_policy")


class TrackError(RuntimeError):
    """A refused or failed tracking operation; the message says what to fix."""


# Everything that means Langfuse refused or could not be reached, as opposed to a bug in Parallax.
LANGFUSE_ERRORS = (TrackError, httpx.HTTPError, ApiError)


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    base_url: str

    def url(self, path: str) -> str:
        return f"{self.base_url}/project/{self.id}/{path.lstrip('/')}"

    def experiment_url(self, experiment_id: str, *, against: str | None = None) -> str:
        """An experiment's item-by-item page; with `against`, side by side with that baseline."""
        compare = f"&c={experiment_id}" if against is not None else ""
        return self.url(f"experiments/results?baseline={against or experiment_id}{compare}")


class StableIds(IdGenerator):
    """Random ids, except the one trace and span `fixed` names: an item's root observation.

    Re-sending an item then rewrites the observation Langfuse already holds instead of adding a
    second one, so a resumed upload cannot duplicate what an earlier attempt wrote.
    """

    def __init__(self) -> None:
        self._random = RandomIdGenerator()
        self._trace: ContextVar[int | None] = ContextVar("parallax_trace_id", default=None)
        self._span: ContextVar[int | None] = ContextVar("parallax_span_id", default=None)

    @contextmanager
    def fixed(self, trace_id: str, span_id: str) -> Generator[None]:
        trace_token, span_token = self._trace.set(int(trace_id, 16)), self._span.set(int(span_id, 16))
        try:
            yield
        finally:
            self._trace.reset(trace_token)
            self._span.reset(span_token)

    def generate_trace_id(self) -> int:
        fixed = self._trace.get()
        if fixed is None:
            return self._random.generate_trace_id()
        self._trace.set(None)
        return fixed

    def generate_span_id(self) -> int:
        fixed = self._span.get()
        if fixed is None:
            return self._random.generate_span_id()
        self._span.set(None)
        return fixed


IDS = StableIds()


def root_ids(experiment_id: str, item_id: str) -> tuple[str, str]:
    """The trace and observation id of one experiment item, the same on every push."""
    digest = hashlib.sha256(f"parallax|{experiment_id}|{item_id}".encode()).hexdigest()
    return digest[:32], digest[32:48]


@functools.cache
def _client(base_url: str, public_key: str, secret_key: str) -> Langfuse:
    """One live client per process and key. The SDK caches its workers by public key and a shut-down
    client's stay stopped, so a second client after `shutdown()` would queue scores that are never sent.
    The SDK shuts it down at exit."""
    # Our own provider, not `id_generator=`: Langfuse ignores that whenever a global provider is
    # already registered, and random ids would make a resumed upload write every item twice.
    provider = TracerProvider(id_generator=IDS)
    # The SDK's 5 s API default fails a whole upload on one slow Langfuse Cloud read during verification.
    return Langfuse(
        base_url=base_url, public_key=public_key, secret_key=secret_key, tracer_provider=provider, timeout=30
    )


def connect(*, write: bool) -> tuple[Langfuse, Project]:
    """A client for the project the SDK variables name; with `write`, only the project PARALLAX_LANGFUSE_PROJECT names."""
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    base_url = (os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST") or "").rstrip("/")
    expected = os.environ.get(PROJECT_ENV)
    if not base_url or not public_key or not secret_key:
        raise TrackError("set LANGFUSE_BASE_URL, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
    client = _client(base_url, public_key, secret_key)
    projects = client.api.projects.get().data
    if len(projects) != 1:
        raise TrackError(f"expected project-scoped keys, got {len(projects)} projects")
    project = projects[0]
    if write and expected is None:
        raise TrackError(
            f"refusing to write to Langfuse project {project.name!r} ({project.id}): set {PROJECT_ENV} to the id "
            "or name of the project eval runs belong in"
        )
    if expected is not None and expected not in (project.id, project.name):
        raise TrackError(f"these keys belong to {project.name!r} ({project.id}), not {expected!r}")
    logger.info("track.connect project=%s (%s) host=%s write=%s", project.name, project.id, base_url, write)
    return client, Project(id=project.id, name=project.name, base_url=base_url)


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

_CATEGORICAL = {"outcome": [o.value for o in Outcome]}
_BOUNDED = {"pass_rate", "partial_credit", "pass_at_1", "pass_all_repeats"}


def _score_configs(client: Langfuse, scores: Sequence[Score]) -> dict[str, str]:
    """Typed score configs, so the UI shows 0-1 rates and the outcome's categories consistently."""
    existing: dict[str, str] = {}
    page = 1
    while True:
        listing = client.api.score_configs.get(page=page, limit=PAGE)
        existing.update({c.name: c.id for c in listing.data if not c.is_archived})
        if listing.meta.total_pages <= page:
            break
        page += 1
    for score in {s.name: s for s in scores}.values():
        if score.name in existing:
            continue
        if score.name in _CATEGORICAL:
            config = client.api.score_configs.create(
                name=score.name,
                data_type=ScoreConfigDataType.CATEGORICAL,
                categories=[ConfigCategory(label=c, value=i) for i, c in enumerate(_CATEGORICAL[score.name])],
            )
        elif score.name in _BOUNDED:
            config = client.api.score_configs.create(
                name=score.name, data_type=ScoreConfigDataType.NUMERIC, min_value=0, max_value=1
            )
        else:
            config = client.api.score_configs.create(name=score.name, data_type=ScoreConfigDataType.NUMERIC)
        existing[score.name] = config.id
    return existing


def _write_items(client: Langfuse, dataset: str, items: Sequence[ItemSpec]) -> None:
    """Upsert every task version the run touched; an item id names one version, so rewriting is harmless."""
    try:
        client.api.datasets.get(dataset_name=quote(dataset, safe=""))
    except NotFoundError:
        client.create_dataset(
            name=dataset,
            description="Parallax tasks; one item per task version. Experiments are Parallax runs.",
            metadata={"managed_by": "parallax"},
        )
    for spec in items:
        client.create_dataset_item(
            dataset_name=dataset, id=spec.id, input=spec.input.model_dump(mode="json"), metadata=spec.metadata
        )
    logger.info("track.items dataset=%s task_versions=%d", dataset, len(items))


def experiment_id_for(project_id: str, dataset_id: str, run_name: str) -> str:
    """The id Langfuse derives for a dataset experiment, so re-runs of one name group into one experiment.

    Byte for byte the SDK runner's `_create_experiment_id`, which must match the server's own derivation.
    """
    payload = json.dumps(
        ["langfuse-experiment-v1", project_id, dataset_id, run_name], separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def metadata_value(value: object) -> str:
    """Experiment metadata as Parallax reads it: strings as they are, anything else as compact JSON, unescaped as Langfuse holds it."""
    return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _score(
    client: Langfuse,
    score: Score,
    configs: dict[str, str],
    at: datetime,
    *,
    trace_id: str | None = None,
    observation_id: str | None = None,
    dataset_run_id: str | None = None,
) -> None:
    # Fixed per target and name, so re-sending a score (a retried push) overwrites it instead of adding one.
    target = observation_id or dataset_run_id or trace_id
    score_id = hashlib.sha256(f"parallax|{target}|{score.name}".encode()).hexdigest()[:32]
    # The run's start, so time-series charts plot a run when it ran rather than when it was pushed.
    match score.value:
        case str() as category:
            client.create_score(
                name=score.name,
                value=category,
                data_type="CATEGORICAL",
                score_id=score_id,
                trace_id=trace_id,
                observation_id=observation_id,
                dataset_run_id=dataset_run_id,
                comment=score.comment,
                config_id=configs.get(score.name),
                timestamp=at,
                environment=EXPERIMENT_ENVIRONMENT,
            )
        case int() | float() as number:
            client.create_score(
                name=score.name,
                value=number,
                data_type="NUMERIC",
                score_id=score_id,
                trace_id=trace_id,
                observation_id=observation_id,
                dataset_run_id=dataset_run_id,
                comment=score.comment,
                config_id=configs.get(score.name),
                timestamp=at,
                environment=EXPERIMENT_ENVIRONMENT,
            )


def _push_run(
    client: Langfuse,
    tracked: TrackedRun,
    dataset_id: str,
    experiment_id: str,
    run: RunSpec,
    configs: dict[str, str],
    evidence: Callable[[str, TraceSpec], bytes],
) -> None:
    """Record one row as a Langfuse experiment: one root observation per item, linked and scored.

    This writes what the SDK's `run_experiment` writes (the experiment span attributes, which
    Langfuse turns into the experiment and its items, and scores) but on a root span named after
    the task and versioned by the row, with the item's evidence attached to its output as a zip.
    Every id is derived from the run, so pushing it again rewrites the same records.
    Nothing is re-executed or re-judged: each item replays what Parallax measured.
    """
    if not run.traces:
        raise TrackError(f"{run.name}: the row has no tasks to record")
    for trace in run.traces:
        output = {
            **trace.output.model_dump(mode="json"),
            EVIDENCE: LangfuseMedia(content_bytes=evidence(run.row, trace), content_type=EVIDENCE_TYPE),
        }
        with (
            IDS.fixed(*root_ids(experiment_id, trace.item_id)),
            propagate_attributes(
                trace_name=trace.name,
                version=trace.version,
                tags=list(trace.tags),
                environment=EXPERIMENT_ENVIRONMENT,
                metadata={"row": run.row, "task_id": trace.task_id},
            ),
            client.start_as_current_observation(
                name=trace.name, as_type="evaluator", input=trace.input.model_dump(mode="json"), output=output
            ) as span,
        ):
            # Metadata travels as one JSON attribute each, as the SDK's runner sends it.
            otel_trace.get_current_span().set_attributes(
                {
                    Attr.EXPERIMENT_ID: experiment_id,
                    Attr.EXPERIMENT_NAME: run.name,
                    Attr.EXPERIMENT_DATASET_ID: dataset_id,
                    Attr.EXPERIMENT_ITEM_ID: trace.item_id,
                    Attr.EXPERIMENT_ITEM_ROOT_OBSERVATION_ID: span.id,
                    Attr.EXPERIMENT_METADATA: json.dumps(run.metadata),
                    Attr.EXPERIMENT_ITEM_METADATA: json.dumps({"task_id": trace.task_id}),
                    **({Attr.EXPERIMENT_DESCRIPTION: run.description} if run.description else {}),
                }
            )
            for score in trace.scores:
                _score(client, score, configs, tracked.created_at, trace_id=span.trace_id, observation_id=span.id)
    for score in run.scores:
        _score(client, score, configs, tracked.created_at, dataset_run_id=experiment_id)


def push(
    client: Langfuse, project: Project, tracked: TrackedRun, evidence: Callable[[str, TraceSpec], bytes]
) -> tuple[tuple[str, str, str], ...]:
    """Write every row of the run and return each experiment's (name, id, page); safe to repeat, since each write
    lands on the same ids."""
    all_scores = [s for run in tracked.runs for s in (*run.scores, *(s for t in run.traces for s in t.scores))]
    configs = _score_configs(client, all_scores)
    _write_items(client, tracked.dataset, tracked.items)
    dataset = client.get_dataset(tracked.dataset)
    experiments: list[tuple[str, str, str]] = []
    for run in tracked.runs:
        experiment_id = experiment_id_for(project.id, dataset.id, run.name)
        _push_run(client, tracked, dataset.id, experiment_id, run, configs, evidence)
        url = project.experiment_url(experiment_id)
        logger.info(
            "track.experiment experiment=%r id=%s items=%d url=%s", run.name, experiment_id, len(run.traces), url
        )
        experiments.append((run.name, experiment_id, url))
    client.flush()
    return tuple(experiments)


# ---------------------------------------------------------------------------
# reads (Langfuse v4 serves experiments only through the experiments API)
# ---------------------------------------------------------------------------


def decode(value: object) -> object:
    """Experiment item input and output arrive JSON-encoded."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def media_bytes(client: Langfuse, reference: object) -> bytes | None:
    """The content behind a Langfuse media reference string, or None when there is none to fetch."""
    if not isinstance(reference, str):
        return None
    try:
        media_id = LangfuseMedia.parse_reference_string(reference)["media_id"]
    except ValueError:
        return None
    response = httpx.get(client.api.media.get(media_id).url, timeout=60)
    return response.content if response.status_code == 200 else None


def mark_verified(client: Langfuse, tracked: TrackedRun, experiments: Sequence[tuple[str, str, str]]) -> None:
    for _, experiment_id, _ in experiments:
        _score(client, Score(VERIFIED, 1.0, "NUMERIC"), {}, tracked.created_at, dataset_run_id=experiment_id)
    client.flush()


@dataclass(frozen=True)
class Listing:
    """The Parallax experiments on one dataset, and the experiments Langfuse holds instead."""

    runs: tuple[RunRecord, ...]
    skipped: int = 0
    missing: tuple[str, ...] = ()  # which of REQUIRED_META the skipped experiments lacked

    def skipped_note(self) -> str | None:
        """A sentence naming the experiments left out and what they lack, or None when there are none."""
        if not self.skipped:
            return None
        if self.skipped == 1:
            return (
                f"1 experiment on this dataset lacks Parallax run metadata ({', '.join(self.missing)}); it was skipped"
            )
        return (
            f"{self.skipped} experiments on this dataset lack Parallax run metadata ({', '.join(self.missing)}); "
            "they were skipped"
        )


def list_runs(client: Langfuse, dataset: str) -> Listing:
    """Every Parallax experiment on the dataset, newest first, and the experiments it left out.

    An experiment Langfuse holds without the metadata compare reads (one written by another tool)
    is counted and its missing keys recorded, rather than silently dropped.
    """
    try:
        dataset_id = client.api.datasets.get(dataset_name=quote(dataset, safe="")).id
    except NotFoundError as exc:
        raise TrackError(
            f"no dataset {dataset!r} in this project: nothing is tracked there yet, or --dataset is wrong"
        ) from exc
    out: list[RunRecord] = []
    skipped = 0
    missing: set[str] = set()
    cursor: str | None = None
    while True:
        page = client.api.experiments.list(
            from_start_time=EPOCH, dataset_id=dataset_id, fields="core,metadata,scores", limit=PAGE, cursor=cursor
        )
        for e in page.data:
            metadata = {k: metadata_value(v) for k, v in (e.metadata or {}).items()}
            absent = [key for key in REQUIRED_META if key not in metadata]
            if absent:
                skipped += 1  # an experiment Parallax did not write, or wrote before a key existed
                missing.update(absent)
                continue
            scores = {sc.name: float(sc.value) for sc in (e.scores or []) if isinstance(sc.value, int | float)}
            # Langfuse dates an experiment by when it was pushed; the run itself says when it ran.
            started = datetime.fromisoformat(metadata["started_at"]) if "started_at" in metadata else e.start_time
            out.append(RunRecord(e.name, e.id, started, metadata, scores, {}))
        cursor = page.meta.cursor
        if not cursor or not page.data:
            break
    ordered = sorted(out, key=lambda r: r.started_at, reverse=True)
    return Listing(tuple(ordered), skipped, tuple(key for key in REQUIRED_META if key in missing))


def _task_record(raw_input: object, raw_output: object) -> TaskRecord | None:
    try:
        task = ItemInput.model_validate(decode(raw_input))
        result = ItemOutput.model_validate(decode(raw_output))
    except ValidationError:
        return None
    return TaskRecord(
        task_id=task.task_id,
        task_version=task.task_version,
        verdicts=tuple(result.verdicts),
        cost_usd=result.cost_usd,
        agent_ms=result.agent_ms,
    )


def experiment_items(client: Langfuse, experiment_id: str, fields: str) -> Iterator[ExperimentItem]:
    cursor: str | None = None
    while True:
        page = client.api.experiments.list_items(
            from_start_time=EPOCH, experiment_id=experiment_id, fields=fields, limit=PAGE, cursor=cursor
        )
        yield from page.data
        cursor = page.meta.cursor
        if not cursor or not page.data:
            return


def load_tasks(client: Langfuse, run: RunRecord) -> RunRecord:
    tasks: dict[str, TaskRecord] = {}
    for item in experiment_items(client, run.id, "core,io"):
        record = _task_record(item.input, item.output)
        if record is None:
            logger.warning("experiment item %s of %r has no Parallax input/output", item.id, run.name)
            continue
        tasks[f"{record.task_id}@v{record.task_version}"] = record
    return RunRecord(run.name, run.id, run.started_at, run.metadata, run.scores, tasks)


def find_runs(runs: Sequence[RunRecord], ref: str) -> list[RunRecord]:
    """Experiments by exact name or id, or every row of a Parallax run by its run id."""
    hits = [r for r in runs if ref in (r.name, r.id, r.metadata.get("run_id"))]
    if not hits:
        raise TrackError(f"no tracked run matches {ref!r}; `parallax show` lists them")
    return hits
