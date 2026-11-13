"""What a sealed run's evidence zips carry: every attempt's trial directory, and each plugin run artifact.

Offline: `seal` and `Evidence` read only the run directory; the Langfuse push is exercised against a live instance.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from parallax import extensions, plugins
from parallax.models import TrialResult, Verdict
from parallax.track.rows import LoadedRun, load_run
from parallax.track.sink import TrackError
from parallax.track.upload import Evidence, seal, upload

R1 = Path(__file__).parent / "fixtures" / "runs" / "r1"
RETRIED = "trials/safety-b/alpha/m1/0-attempt1"


@dataclass(frozen=True)
class FakeEntryPoint:
    name: str
    target: object
    value: str = "fake.module:obj"

    def load(self) -> object:
        return self.target


@pytest.fixture
def artifacts(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[FakeEntryPoint]]:
    registered: list[FakeEntryPoint] = []
    monkeypatch.setattr(
        plugins, "entry_points_in", lambda group: registered if group == extensions.RUN_ARTIFACTS_GROUP else []
    )
    yield registered


def run_with_a_retry(tmp_path: Path) -> Path:
    """r1 with safety-b/alpha/m1 #0 attempted twice: the first attempt's directory stays beside the second's."""
    run = tmp_path / "r1"
    shutil.copytree(R1, run)
    rows = [TrialResult.model_validate_json(line) for line in (run / "results.jsonl").read_text().splitlines()]
    first = next(r for r in rows if (r.task_id, r.harness, r.repeat) == ("safety-b", "alpha", 0))
    retry = first.model_copy(update={"attempt": 1, "dir": RETRIED, "verdict": Verdict.PASS})
    (run / RETRIED).mkdir(parents=True)
    (run / RETRIED / "trajectory.json").write_text("{}")
    with (run / "results.jsonl").open("a") as fh:
        fh.write(retry.model_dump_json() + "\n")
    return run


def members(zipped: bytes) -> set[str]:
    return set(zipfile.ZipFile(io.BytesIO(zipped)).namelist())


def test_every_attempts_directory_travels_in_its_items_evidence(
    tmp_path: Path, artifacts: list[FakeEntryPoint]
) -> None:
    run = run_with_a_retry(tmp_path)
    loaded = load_run(run)
    evidence = Evidence(run, seal(run, dataset="ds", note="", meta={}), loaded.results)
    held = set().union(*(members(z) for z in evidence.zips.values()))
    assert {f"{RETRIED}/trajectory.json", "trials/safety-b/alpha/m1/0/trajectory.json"} <= held


def summary(run: LoadedRun) -> bytes:
    return "".join(f"{r.task_id} {r.verdict.value}\n" for r in sorted(run.results, key=lambda r: r.key)).encode()


def test_a_run_artifact_is_sealed_and_carried_by_every_item(tmp_path: Path, artifacts: list[FakeEntryPoint]) -> None:
    artifacts.append(FakeEntryPoint("summary", extensions.RunArtifact(path="summary.txt", build=summary)))
    run = tmp_path / "r1"
    shutil.copytree(R1, run)
    sealed = seal(run, dataset="ds", note="", meta={})
    assert "summary.txt" in sealed.files and (run / "summary.txt").read_bytes() == summary(load_run(run))
    evidence = Evidence(run, sealed, load_run(run).results)
    assert all("summary.txt" in members(z) for z in evidence.zips.values())


@pytest.mark.parametrize("path", ["../outside.txt", "trials/x.txt", "results.jsonl", "/abs.txt"])
def test_a_run_artifact_may_not_write_outside_its_place(
    tmp_path: Path, artifacts: list[FakeEntryPoint], path: str
) -> None:
    artifacts.append(FakeEntryPoint("bad", extensions.RunArtifact(path=path, build=summary)))
    run = tmp_path / "r1"
    shutil.copytree(R1, run)
    with pytest.raises(TrackError, match="must be relative"):
        seal(run, dataset="ds", note="", meta={})


def test_an_artifact_that_rebuilds_differently_is_never_uploaded(
    tmp_path: Path, artifacts: list[FakeEntryPoint]
) -> None:
    builds = iter([b"first", b"second"])
    artifacts.append(FakeEntryPoint("clock", extensions.RunArtifact(path="clock.txt", build=lambda run: next(builds))))
    run = tmp_path / "r1"
    shutil.copytree(R1, run)
    seal(run, dataset="ds", note="", meta={})
    with pytest.raises(TrackError, match="rebuilds to other bytes"):
        upload(run)
