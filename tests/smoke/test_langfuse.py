"""Smoke: run controls, record them in a throwaway self-hosted Langfuse, read every field back.

The first upload is killed partway; `parallax upload --resume` finishes it without a duplicate. The
workflow `parallax ci init` writes is then executed step by step, as GitHub would, for a push to main
and a pull request. Needs Docker; opt in with `-m langfuse`. The stack is started and removed here,
which also proves that no container, process or pending run survives.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml
from langfuse import Langfuse, LangfuseMedia
from parallax import provenance
from parallax.ci import write_workflow
from parallax.cli import OK, main
from parallax.models import CostSource, Isolation, Verdict
from parallax.runner import RunOptions, latest_results, run_async, serve_embedded
from parallax.task import resolve
from parallax.track.compare import VERIFIED
from parallax.track.projection import TrackedRun, project
from parallax.track.rows import load_run
from parallax.track.sink import EPOCH, EVIDENCE, connect, experiment_id_for, list_runs, metadata_value, root_ids
from parallax.track.upload import SEAL, pending, seal
from tests.smoke.judge_replay import JudgeReplay
from tests.smoke.langfuse_stack import Stack, langfuse_stack, remaining
from tests.smoke.test_controls import FALLBACK_MODEL, TASK

DATASET = "parallax-smoke"
HARNESSES = ("oracle", "null-claim")
KILL_WITHIN_S = 300


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with langfuse_stack() as started:
        yield started
    assert remaining(started.project) == []


@pytest.fixture
def langfuse(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> Stack:
    monkeypatch.setenv("LANGFUSE_BASE_URL", stack.base_url)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", stack.public_key)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", stack.secret_key)
    monkeypatch.setenv("PARALLAX_LANGFUSE_PROJECT", "parallax-smoke")
    return stack


@asynccontextmanager
async def _judges(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Recorded judge replies on a local port, for this process and the CLI processes it starts."""
    record_key = os.environ.get("PARALLAX_SMOKE_RECORD_KEY")
    replay = JudgeReplay(record_key)
    server = await serve_embedded(replay.app, "127.0.0.1")
    url = f"http://127.0.0.1:{server.port}"
    try:
        monkeypatch.setenv("OPENROUTER_API_KEY", "replay")
        monkeypatch.setenv("PARALLAX_JEV_BASE_URL", url)
        monkeypatch.setenv("PARALLAX_JUDGE_BASE_URL", url)
        monkeypatch.setenv("PARALLAX_JUDGE_API_KEY", "replay")
        monkeypatch.setenv("PARALLAX_JUDGE_MODELS", FALLBACK_MODEL)
        yield
    finally:
        await server.stop()
    if record_key is not None:
        replay.save()
    assert not replay.misses, f"unrecorded judge requests {replay.misses}: re-record with PARALLAX_SMOKE_RECORD_KEY"


def _run_controls(monkeypatch: pytest.MonkeyPatch, out: Path) -> Path:
    async def run() -> Path:
        async with _judges(monkeypatch):
            opts = RunOptions(out=out, isolation=Isolation.HOST, selection="smoke")
            return await run_async(resolve([str(TASK)], []), HARNESSES, (None,), opts)

    return asyncio.run(run())


def _kill_during_verification(root: Path) -> None:
    """Start `parallax upload --resume` and SIGKILL it once everything is sent but not yet verified."""
    cli = Path(sys.executable).parent / "parallax"
    proc = subprocess.Popen(
        [str(cli), "upload", "--resume", "--root", str(root)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    timer = threading.Timer(KILL_WITHIN_S, proc.kill)
    timer.start()
    try:
        assert proc.stderr is not None
        log: list[str] = []
        for line in proc.stderr:
            log.append(line)
            if "track.verify" in line:
                proc.send_signal(signal.SIGKILL)
                break
        proc.wait(timeout=30)
    finally:
        timer.cancel()
    assert proc.returncode == -signal.SIGKILL, "".join(log)


def _evidence(client: Langfuse, reference: object) -> dict[str, bytes]:
    assert isinstance(reference, str), reference
    media = client.api.media.get(LangfuseMedia.parse_reference_string(reference)["media_id"])
    reply = httpx.get(media.url, timeout=60)
    reply.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(reply.content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _read_back(client: Langfuse, project_id: str, tracked: TrackedRun, original: Path) -> None:
    """Everything the run measured is in Langfuse exactly once, and the evidence is byte-identical."""
    dataset = client.get_dataset(DATASET)
    assert sorted(i.id for i in dataset.items) == sorted(i.id for i in tracked.items)
    recorded = {r.id: r for r in list_runs(client, DATASET).runs}
    assert len(recorded) == len(tracked.runs) == len(HARNESSES)
    files = {p.relative_to(original).as_posix(): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    carried: set[str] = set()
    for run in tracked.runs:
        experiment_id = experiment_id_for(project_id, dataset.id, run.name)
        held = recorded[experiment_id]
        assert held.metadata == {k: metadata_value(v) for k, v in run.metadata.items()}
        assert held.scores == {**{s.name: s.value for s in run.scores}, VERIFIED: 1.0}
        items = client.api.experiments.list_items(
            from_start_time=EPOCH, experiment_id=experiment_id, fields="core,io,scores", limit=100
        ).data
        assert sorted(i.experiment_item_id for i in items) == sorted(t.item_id for t in run.traces)
        for trace in run.traces:
            (item,) = [i for i in items if i.experiment_item_id == trace.item_id]
            assert (item.trace_id, item.id) == root_ids(experiment_id, trace.item_id)
            output = json.loads(item.output) if isinstance(item.output, str) else dict(item.output)
            evidence = _evidence(client, output.pop(EVIDENCE))
            assert output == trace.output.model_dump(mode="json")
            assert {s.name: s.value for s in item.scores or []} == {s.name: s.value for s in trace.scores}
            for repeat in trace.output.repeats:
                # Controls report no usage: the cost stays unknown, never a measured zero.
                assert repeat.wall_ms is not None and repeat.agent_ms is not None
                assert (repeat.cost_usd, repeat.cost_source) == (None, CostSource.NONE)
            assert trace.output.cost_usd is None
            for name, data in evidence.items():
                if name != "results.jsonl":
                    assert data == files[name], name
                    carried.add(name)
            assert evidence["results.jsonl"].decode().splitlines()
    assert files.keys() - {SEAL} <= carried | {"results.jsonl"}


@pytest.mark.langfuse
def test_run_is_recorded_resumed_and_read_back(
    langfuse: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "runs"
    run_dir = _run_controls(monkeypatch, root / "smoke")
    local = {r.harness: r for r in latest_results(run_dir / "results.jsonl")}
    assert (local["oracle"].verdict, local["null-claim"].verdict) == (Verdict.PASS, Verdict.FAIL)
    seal(run_dir, dataset=DATASET, note="smoke", meta={"branch": "main"})
    head = provenance.read(run_dir).parallax_head
    tracked = project(load_run(run_dir), dataset=DATASET, note="smoke", extra={"branch": "main"}, parallax_head=head)
    original = tmp_path / "original"
    shutil.copytree(run_dir, original)

    _kill_during_verification(root)
    assert pending(root) == [run_dir], "a killed upload must keep the run for --resume"
    client, project_ = connect(write=False)
    assert not any(VERIFIED in r.scores for r in list_runs(client, DATASET).runs), "a half-uploaded run is no baseline"
    assert main(["upload", "--resume", "--root", str(root)]) == OK
    assert pending(root) == [] and not run_dir.exists()
    deadline = time.monotonic() + 60
    while not all(VERIFIED in r.scores for r in list_runs(client, DATASET).runs):  # scores land asynchronously
        assert time.monotonic() < deadline, "the resumed upload never marked its run verified"
        time.sleep(1)

    _read_back(client, project_.id, tracked, original)


GH_STUB = """#!/bin/sh
# Stands in for gh: keeps the body the workflow posts or edits, and lists it as comment 1 once posted.
log="$GH_COMMENT.calls"
case "$*" in
  *--jq*) [ -f "$GH_COMMENT" ] && echo 1; exit 0 ;;
esac
for arg in "$@"; do
  case "$prev" in --body-file) cp "$arg" "$GH_COMMENT"; echo create >> "$log" ;; esac
  case "$arg" in body=@*) cp "${arg#body=@}" "$GH_COMMENT"; echo edit >> "$log" ;; esac
  prev=$arg
done
"""


@pytest.mark.langfuse
def test_ci_workflow_records_main_and_comments_the_pull_request_delta(
    langfuse: Stack, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_workflow(
        tmp_path / "parallax.yml", suite=str(TASK), harness="oracle", model=None, repeats=1, install="",
        isolation="host", branch="main", force=False,
    )  # fmt: skip
    steps = {s.get("name"): s for s in yaml.safe_load(path.read_text())["jobs"]["evaluate"]["steps"]}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(GH_STUB)
    (bin_dir / "gh").chmod(0o755)
    comment = tmp_path / "comment.md"

    def step(name: str, branch: str, run_id: str) -> None:
        script = steps[name]["run"]
        env = {
            **os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_COMMENT": str(comment),
            "PARALLAX": str(Path(sys.executable).parent / "parallax"), "BRANCH": branch, "RUN_ID": run_id, "PR": "7",
            "GITHUB_SHA": "0" * 40, "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"), "RUN_URL": "https://ci/run",
        }  # fmt: skip
        done = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True, timeout=900)
        assert done.returncode == 0, done.stdout + done.stderr

    async def both() -> None:
        async with _judges(monkeypatch):
            await asyncio.to_thread(step, "Evaluate", "main", "gh-1-1")
            await asyncio.to_thread(step, "Evaluate", "feature", "gh-2-1")
            await asyncio.to_thread(step, "Report", "feature", "gh-2-1")
            await asyncio.to_thread(step, "Compare with main", "feature", "gh-2-1")
            await asyncio.to_thread(step, "Compare with main", "feature", "gh-2-1")

    asyncio.run(both())
    assert Path(f"{comment}.calls").read_text().split() == ["create", "edit"]
    # Every run reached Langfuse, so the local buffer is empty: it only ever holds what is not uploaded.
    assert [p for p in (tmp_path / "runs").rglob("*") if p.is_file()] == []
    body = comment.read_text()
    assert "#### Results: `gh-2-1`" in body and "**1 pass** of 1 trials" in body, body
    assert "#### Against the newest main run" in body and "[Workflow run](https://ci/run)" in body, body
    assert "no baseline run" not in body, body
