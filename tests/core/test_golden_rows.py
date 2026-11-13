"""Serialized results never move under an additive change: every byte a world-task run writes is pinned here.

The golden file was written by this module against the code before the browser seams existed
(`PARALLAX_REGEN_GOLDEN=1 uv run pytest tests/core/test_golden_rows.py`). Regenerate it only for a change that
is meant to alter stored rows, and say so in the pull request. It was regenerated once since, for `Capability.BROWSER`:
a null control declares every capability, so its `config_hash` (and with it each control row's resume key) moved.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest
from parallax import runner, semantic
from parallax.models import CheckResult, FailureMode, Isolation, RunManifest, TrialResult
from parallax.runner import RunOptions, read_results, run_async
from parallax.semantic import Graded
from parallax.task import discover
from parallax.track.projection import project
from parallax.track.rows import load_run
from parallax.track.upload import Evidence, seal

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden" / "rows.json"
# Timings vary per run; a control reports Parallax's own version, and the key hashes it, so both move every release.
VOLATILE = ("key", "boot_ms", "ttft_ms", "wall_ms", "agent_ms", "harness_version")


def _r1() -> dict[str, object]:
    run = FIXTURES / "runs" / "r1"
    lines = [line for line in (run / "results.jsonl").read_text().splitlines() if line.strip()]
    redumped = [TrialResult.model_validate_json(line).model_dump_json() for line in lines]
    manifest = RunManifest.model_validate_json((run / "manifest.json").read_text()).model_dump_json(indent=2)
    tracked = project(load_run(run), dataset="golden")
    return {
        "results_sha256": hashlib.sha256("\n".join(redumped).encode()).hexdigest(),
        "results_unchanged": redumped == lines,
        "manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
        "projection_sha256": hashlib.sha256(
            json.dumps(asdict(tracked), sort_keys=True, default=_jsonable).encode()
        ).hexdigest(),
    }


def _jsonable(value: object) -> object:
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else str(value)


def _evidence(tmp: Path) -> dict[str, str]:
    run = tmp / "r1"
    shutil.copytree(FIXTURES / "runs" / "r1", run)
    sealed = seal(run, dataset="golden", note="", meta={})
    evidence = Evidence(run, sealed, load_run(run).results)
    return {f"{row}::{item}": hashlib.sha256(data).hexdigest() for (row, item), data in sorted(evidence.zips.items())}


async def _live(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    async def canned(*_: object, **__: object) -> Graded:
        return Graded(
            checks=(
                CheckResult(
                    id="c", kind="state", mode=FailureMode.MISSING_ACTION, gate=True, advisory=False,
                    weight=1.0, passed=True, detail="",
                ),
            )
        )  # fmt: skip

    monkeypatch.setattr(runner, "grade_checks", canned)
    out = await run_async(
        discover([FIXTURES / "tasks"]),
        ["null-claim"],
        [None],
        RunOptions(out=tmp / "live", isolation=Isolation.HOST, quiet=True),
    )
    rows = sorted(read_results(out / "results.jsonl"), key=lambda r: r.task_id)
    return [{k: v for k, v in json.loads(r.model_dump_json()).items() if k not in VOLATILE} for r in rows]


async def test_world_task_rows_and_their_projection_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = {
        "r1": _r1(),
        "evidence_zip_sha256": _evidence(tmp_path),
        "live_rows": await _live(tmp_path, monkeypatch),
        "policy_identity": semantic.policy_identity(),
    }
    if os.environ.get("PARALLAX_REGEN_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    assert current == json.loads(GOLDEN.read_text())
