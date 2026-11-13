"""The upload buffer: a finished run waits on disk only until Langfuse provably holds all of it.

`seal` freezes a finished run directory: it records every file with its hash and the dataset it
belongs in. `upload` pushes the sealed run (every write lands on a fixed id, so a repeat cannot
duplicate), reads it back field for field, downloads each evidence zip and checks its bytes, and
deletes the directory only when everything matches. A run directory without a seal is still
being written and is never uploaded.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import logging
import math
import shutil
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from langfuse import Langfuse
from langfuse.api import ExperimentItem
from pydantic import BaseModel

from parallax import extensions, provenance
from parallax.models import TrialResult
from parallax.track.projection import TraceSpec, TrackedRun, item_id, project
from parallax.track.rows import group_rows, load_run
from parallax.track.sink import (
    EPOCH,
    EVIDENCE,
    Project,
    TrackError,
    connect,
    decode,
    experiment_id_for,
    experiment_items,
    mark_verified,
    media_bytes,
    metadata_value,
    push,
    root_ids,
)

logger = logging.getLogger(__name__)

SEAL = "upload.json"
LOCK = ".upload.lock"
# Files every item's evidence carries: what the run was, where it ran, and (for a budgeted run) its spend journal.
RUN_FILES = ("manifest.json", "provenance.json", "budget.jsonl")
RESULTS = "results.jsonl"
TRIALS = "trials"
# Langfuse makes writes readable a few seconds after they land; a read-back waits this long for them.
VISIBLE_WITHIN_S = 180.0
POLL_S = 3.0
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class Seal(BaseModel):
    """A finished run, frozen for upload: its files are checked against these hashes before anything is sent."""

    run_id: str
    dataset: str
    note: str
    meta: dict[str, str]  # extra experiment metadata, e.g. the branch a CI run measured
    files: dict[str, str]  # path relative to the run directory -> sha256


@dataclass(frozen=True)
class Uploaded:
    run_dir: Path
    experiments: tuple[tuple[str, str, str], ...]  # (experiment name, id, its page)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _inventory(run_dir: Path) -> dict[str, str]:
    return {
        p.relative_to(run_dir).as_posix(): _sha256(p)
        for p in sorted(run_dir.rglob("*"))
        # Only the run's own control files: a trial workspace may hold files with the same names.
        if p.is_file() and p.relative_to(run_dir).as_posix() not in (SEAL, LOCK)
    }


def artifact_files(run_dir: Path) -> dict[str, bytes]:
    """Every plugin run artifact (`extensions.RunArtifact`) built from the run as it stands."""
    artifacts = extensions.run_artifacts()
    if not artifacts:
        return {}
    reserved = {*RUN_FILES, RESULTS, SEAL, LOCK}
    loaded = load_run(run_dir)
    built: dict[str, bytes] = {}
    for artifact in artifacts:
        rel = Path(artifact.path)
        if rel.is_absolute() or ".." in rel.parts or rel.parts[0] == TRIALS or artifact.path in reserved:
            raise TrackError(f"run artifact path {artifact.path!r} must be relative, outside trials/, and not reserved")
        built[rel.as_posix()] = artifact.build(loaded)
    return built


def seal(run_dir: Path, *, dataset: str, note: str, meta: Mapping[str, str]) -> Seal:
    """Freeze a finished run for upload, plugin run artifacts written first so the seal hashes them. Written last and
    atomically: a seal means the run is complete."""
    for name, data in artifact_files(run_dir).items():
        (run_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (run_dir / name).write_bytes(data)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    sealed = Seal(run_id=manifest["run_id"], dataset=dataset, note=note, meta=dict(meta), files=_inventory(run_dir))
    tmp = run_dir / f".{SEAL}.tmp"
    tmp.write_text(sealed.model_dump_json(indent=2))
    tmp.replace(run_dir / SEAL)
    return sealed


def pending(root: Path) -> list[Path]:
    """Sealed runs under `root` that Langfuse does not yet hold."""
    return sorted(p.parent for p in root.glob(f"*/{SEAL}"))


@contextmanager
def owned(run_dir: Path) -> Iterator[None]:
    """One uploader per run: a second one exits rather than racing the first."""
    try:
        fh = (run_dir / LOCK).open("w")
    except FileNotFoundError:
        raise TrackError(f"{run_dir}: already uploaded by another process") from None
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TrackError(f"{run_dir}: another upload of this run is in progress") from None
        if not (run_dir / SEAL).is_file():
            raise TrackError(f"{run_dir}: already uploaded by another process")
        yield


def _trials_by_item(results: Sequence[TrialResult], dataset: str) -> dict[tuple[str, str], list[TrialResult]]:
    out: dict[tuple[str, str], list[TrialResult]] = {}
    for label, trials in group_rows(results).items():
        for t in trials:
            out.setdefault((label, item_id(dataset, t.task_id, t.task_version)), []).append(t)
    return out


def _zip(files: Mapping[str, bytes]) -> bytes:
    """Byte-identical for identical files, so a repeated upload re-sends the same media."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])
    return buffer.getvalue()


class Evidence:
    """Each item's zip: its trials' directories and result rows (every attempt's), the run's own files and every
    plugin run artifact."""

    def __init__(self, run_dir: Path, sealed: Seal, results: Sequence[TrialResult]) -> None:
        self.run_dir = run_dir
        self.trials = _trials_by_item(results, sealed.dataset)
        lines = (run_dir / RESULTS).read_text().splitlines()
        self.rows: dict[str, list[str]] = {}
        dirs: dict[str, list[str]] = {}  # key -> every attempt's trial directory
        for line in lines:
            if line.strip():
                row = TrialResult.model_validate_json(line)
                self.rows.setdefault(row.key, []).append(line)
                if row.dir is not None:
                    dirs.setdefault(row.key, []).append(row.dir)
        self.zips: dict[tuple[str, str], bytes] = {}
        shared = [*RUN_FILES, *sorted(Path(a.path).as_posix() for a in extensions.run_artifacts())]
        covered = {f for f in shared if f in sealed.files} | {RESULTS}
        for key, trials in self.trials.items():
            files = {name: (run_dir / name).read_bytes() for name in shared if name in sealed.files}
            files[RESULTS] = "".join(f"{row}\n" for t in trials for row in self.rows.get(t.key, ())).encode()
            for t in trials:
                for prefix in (f"{d}/" for d in dirs.get(t.key, ())):
                    for name in sealed.files:
                        if name.startswith(prefix):
                            files[name] = (run_dir / name).read_bytes()
                            covered.add(name)
            self.zips[key] = _zip(files)
        if missing := sorted(set(sealed.files) - covered):
            # Langfuse is the only store: a file no item carries would be lost with the directory.
            raise TrackError(f"{run_dir}: no task's evidence holds {', '.join(missing[:5])}; nothing was uploaded")

    def __call__(self, row: str, trace: TraceSpec) -> bytes:
        return self.zips[(row, trace.item_id)]


def _close(a: object, b: object) -> bool:
    if isinstance(a, float | int) and isinstance(b, float | int) and not isinstance(a, bool):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    return a == b


def mismatches(
    client: Langfuse, project: Project, tracked: TrackedRun, evidence: Callable[[str, TraceSpec], bytes]
) -> list[str]:
    """Every way what Langfuse holds differs from the run; empty when it holds all of it exactly.

    `evidence` is what `sink.push` wrote: an `Evidence`, or a plugin's own function from row and trace to zip bytes."""
    out: list[str] = []
    dataset_id = client.get_dataset(tracked.dataset).id
    for run in tracked.runs:
        experiment_id = experiment_id_for(project.id, dataset_id, run.name)
        listing = client.api.experiments.list(
            from_start_time=EPOCH, id=experiment_id, fields="metadata,scores", limit=1
        )
        if not listing.data:
            out.append(f"{run.name}: no experiment")
            continue
        (experiment,) = listing.data
        stored = {k: metadata_value(v) for k, v in (experiment.metadata or {}).items()}
        wanted = {k: metadata_value(v) for k, v in run.metadata.items()}
        if differ := sorted(k for k in stored.keys() | wanted.keys() if stored.get(k) != wanted.get(k)):
            out.append(f"{run.name}: metadata differs at " + ", ".join(f"{k}={stored.get(k)!r}" for k in differ))
        held = {s.name: s.value for s in experiment.scores or []}
        out += [
            f"{run.name}: score {s.name} is {held.get(s.name)!r}, not {s.value!r}"
            for s in run.scores
            if not _close(held.get(s.name), s.value)
        ]
        items: dict[str, list[ExperimentItem]] = {}
        for item in experiment_items(client, experiment_id, "core,io,scores"):
            items.setdefault(item.experiment_item_id, []).append(item)
        if extra := sorted(set(items) - {t.item_id for t in run.traces}):
            out.append(f"{run.name}: items Parallax did not write: {', '.join(extra)}")
        for trace in run.traces:
            found = items.get(trace.item_id, [])
            if len(found) != 1:
                out.append(f"{run.name}/{trace.task_id}: {len(found)} items, not 1")
                continue
            (item,) = found
            trace_id, span_id = root_ids(experiment_id, trace.item_id)
            if (item.trace_id, item.id) != (trace_id, span_id):
                out.append(f"{run.name}/{trace.task_id}: written under another id")
            output = decode(item.output)
            if not isinstance(output, dict):
                out.append(f"{run.name}/{trace.task_id}: no output")
                continue
            reference = output.pop(EVIDENCE, None)
            if output != trace.output.model_dump(mode="json"):
                out.append(f"{run.name}/{trace.task_id}: output differs")
            scores = {s.name: s.value for s in item.scores or []}
            out += [
                f"{run.name}/{trace.task_id}: score {s.name} is {scores.get(s.name)!r}, not {s.value!r}"
                for s in trace.scores
                if not _close(scores.get(s.name), s.value)
            ]
            expected = hashlib.sha256(evidence(run.row, trace)).hexdigest()
            if (content := media_bytes(client, reference)) is None or hashlib.sha256(content).hexdigest() != expected:
                out.append(f"{run.name}/{trace.task_id}: evidence missing or not byte-identical")
    return out


def upload(run_dir: Path, *, timeout_s: float = VISIBLE_WITHIN_S) -> Uploaded:
    """Push one sealed run, prove Langfuse holds all of it, then delete it. Raises, keeping the run, otherwise."""
    with owned(run_dir):
        sealed = Seal.model_validate_json((run_dir / SEAL).read_text())
        if (now := _inventory(run_dir)) != sealed.files:
            changed = sorted(set(now.items()) ^ set(sealed.files.items()))
            raise TrackError(f"{run_dir}: changed after it was sealed ({changed[0][0]}); not uploaded")
        for name, data in artifact_files(run_dir).items():
            if sealed.files.get(name) != hashlib.sha256(data).hexdigest():
                raise TrackError(
                    f"{run_dir}: run artifact {name} rebuilds to other bytes than were sealed; not uploaded"
                )
        loaded = load_run(run_dir)
        head = provenance.read(run_dir).parallax_head
        tracked = project(loaded, dataset=sealed.dataset, note=sealed.note, extra=sealed.meta, parallax_head=head)
        evidence = Evidence(run_dir, sealed, loaded.results)
        client, project_ = connect(write=True)
        experiments = push(client, project_, tracked, evidence)
        deadline = time.monotonic() + timeout_s
        while problems := mismatches(client, project_, tracked, evidence):
            if time.monotonic() > deadline:
                shown = "; ".join(problems[:5])
                raise TrackError(f"{run_dir}: Langfuse does not hold the run yet ({shown}); kept for --resume")
            logger.info("track.verify run=%s waiting on %d difference(s)", sealed.run_id, len(problems))
            time.sleep(POLL_S)
        mark_verified(client, tracked, experiments)
        logger.info("track.verified run=%s experiments=%d", sealed.run_id, len(experiments))
        # Still under the lock, and out of `pending` in one atomic step before anything is deleted.
        gone = run_dir.with_name(f".{run_dir.name}.uploaded")
        shutil.rmtree(gone, ignore_errors=True)
        run_dir.rename(gone)
        shutil.rmtree(gone)
    return Uploaded(run_dir=run_dir, experiments=experiments)
