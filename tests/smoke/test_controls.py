"""Smoke: one task's deterministic controls, judged by the real policy through recorded judge replies."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from parallax.controls import ControlsReport, check_async
from parallax.models import Isolation, Verdict
from parallax.runner import serve_embedded
from parallax.task import resolve
from tests.smoke.judge_replay import JudgeReplay

TASK = Path(__file__).parents[2] / "tasks" / "conformance" / "conf-mcp-echo"
FALLBACK_MODEL = "deepseek/deepseek-v4.1-flash-1017"


def test_controls_pass_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    record_key = os.environ.get("PARALLAX_SMOKE_RECORD_KEY")
    replay = JudgeReplay(record_key)
    monkeypatch.chdir(tmp_path)  # check writes its evidence under ./runs

    async def run() -> ControlsReport:
        server = await serve_embedded(replay.app, "127.0.0.1")
        url = f"http://127.0.0.1:{server.port}"
        try:
            # The replay never forwards these keys; they only satisfy the judges' configuration checks.
            monkeypatch.setenv("OPENROUTER_API_KEY", "replay")
            monkeypatch.setenv("PARALLAX_JEV_BASE_URL", url)
            monkeypatch.setenv("PARALLAX_JUDGE_BASE_URL", url)
            monkeypatch.setenv("PARALLAX_JUDGE_API_KEY", "replay")
            monkeypatch.setenv("PARALLAX_JUDGE_MODELS", FALLBACK_MODEL)
            return await check_async(resolve([str(TASK)], []), Isolation.HOST)
        finally:
            await server.stop()

    report = asyncio.run(run())
    if record_key is not None:
        replay.save()
    assert not replay.misses, f"unrecorded judge requests {replay.misses}: re-record with PARALLAX_SMOKE_RECORD_KEY"
    (task,) = report.tasks
    assert (task.oracle, task.null_claim, task.null_refuse) == (Verdict.PASS, Verdict.FAIL, Verdict.FAIL), task.problems
    assert task.mutants and all(m.ok for m in task.mutants), task.mutants
