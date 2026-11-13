"""`parallax report`: a run's tally and every trial that did not pass, with why."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import yaml
from parallax.cli import OK, main
from parallax.models import Verdict
from parallax.track.compare import RunRecord
from parallax.track.projection import ItemInput, ItemOutput
from parallax.track.report import evidence_reasons, render

REASON = "semantic judge rejected execution validity: the send hit an approval card"


def item(task_id: str, verdict: Verdict, *, failed: str | None = None, reason: str | None = None):
    repeat = {
        "repeat": 0, "verdict": verdict.value, "score": float(verdict is Verdict.PASS), "cost_usd": 0.05,
        "agent_ms": 1000, "wall_ms": 1000, "invalid_reason": None, "flags": [],
        "invalid_reason_sha256": hashlib.sha256(reason.encode()).hexdigest() if reason else None,
        "status": "harness_error" if verdict is Verdict.INVALID else "completed",
        "checks": [
            {"id": c, "kind": "state", "mode": "wrong_output", "gate": False, "advisory": False, "weight": 1.0,
             "passed": c != failed}
            for c in ("kept-scope", failed) if c
        ],
    }  # fmt: skip
    output = {
        "outcome": verdict.value if verdict in (Verdict.PASS, Verdict.FAIL) else "invalid", "verdicts": [], "failed_checks": [], "cost_usd": 0.05, "agent_ms": 1000.0,
        "harness_version": "1", "config_hash": "c", "actual_models": [], "repeats": [repeat],
    }  # fmt: skip
    return ItemInput(task_id=task_id, task_version=1), ItemOutput.model_validate(output)


def evidence(*reasons: str | None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("results.jsonl", "".join(json.dumps({"invalid_reason": r}) + "\n" for r in reasons))
    return buffer.getvalue()


RUN = RunRecord("run", "exp1", datetime(2026, 11, 8, tzinfo=UTC), {"run_id": "gh-1-1", "row": "agent|fast"}, {}, {})
ITEMS = [
    item("approval", Verdict.INVALID, reason=REASON),
    item("at-risk-deals", Verdict.PASS),
    item("vip-scope", Verdict.FAIL, failed="drafted-all"),
]


def test_the_report_tallies_the_run_and_says_why_each_trial_missed() -> None:
    text = render(RUN, ITEMS, [("Langfuse experiment", "https://lf/exp1")])
    assert "#### Results: `gh-1-1` (agent/fast)" in text
    assert "**1 pass, 1 fail, 1 invalid** of 3 trials over 3 tasks, 0.15 USD." in text
    assert "| approval@v1 | #0 | invalid | harness_error |" in text  # Langfuse holds only the reason's hash
    assert "| vip-scope@v1 | #0 | fail | failed `drafted-all` |" in text
    assert "at-risk-deals" not in text
    assert text.endswith("[Langfuse experiment](https://lf/exp1)\n")


def test_reasons_come_from_the_evidence_by_the_hash_langfuse_records() -> None:
    reasons = evidence_reasons(evidence(None, "an earlier attempt's reason", REASON))
    text = render(RUN, ITEMS, [], reasons)
    assert f"| approval@v1 | #0 | invalid | harness_error: {REASON} |" in text
    assert "earlier attempt" not in text


def test_the_webhook_step_signs_the_report_as_the_receiver_checks_it(tmp_path: Path) -> None:
    path = tmp_path / "parallax.yml"
    assert main(["ci", "init", "--suite", "core", "--harness", "oracle", "--path", str(path)]) == OK
    steps = {s.get("name"): s for s in yaml.safe_load(path.read_text())["jobs"]["evaluate"]["steps"]}
    assert steps["Report"]["continue-on-error"] is True
    step = steps["Send the report to a webhook"]
    assert "pull_request" in step["if"]  # pull requests report on their own comment, not the webhook
    (tmp_path / "report.md").write_text("#### Results\n")
    curl = tmp_path / "curl"
    curl.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done > curl.args\n')
    curl.chmod(0o755)
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}", "WEBHOOK_URL": "https://hook", "WEBHOOK_SECRET": "s3cret",
        "GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_NAME": "push", "RUN_ID": "gh-1-1", "BRANCH": "main", "RUN_URL": "u",
    }  # fmt: skip
    subprocess.run(["bash", "-e", "-c", step["run"]], env=env, cwd=tmp_path, check=True)
    body = (tmp_path / "payload.json").read_bytes()
    assert json.loads(body)["report_markdown"] == "#### Results\n"
    signature = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert f"X-Webhook-Signature: {signature}" in (tmp_path / "curl.args").read_text().splitlines()


def test_without_a_webhook_the_step_sends_nothing(tmp_path: Path) -> None:
    path = tmp_path / "parallax.yml"
    assert main(["ci", "init", "--suite", "core", "--harness", "oracle", "--path", str(path)]) == OK
    steps = {s.get("name"): s for s in yaml.safe_load(path.read_text())["jobs"]["evaluate"]["steps"]}
    env = {"PATH": os.environ["PATH"], "WEBHOOK_URL": "", "WEBHOOK_SECRET": ""}
    subprocess.run(
        ["bash", "-e", "-c", steps["Send the report to a webhook"]["run"]], env=env, cwd=tmp_path, check=True
    )
    assert not (tmp_path / "payload.json").exists()


def test_unknown_costs_and_unscored_trials_are_never_reported_as_zero_or_failed() -> None:
    unmetered = item("unmetered", Verdict.PASS)
    unmetered[1].repeats[0].cost_usd = None
    text = render(RUN, [*ITEMS, unmetered, item("desktop-only", Verdict.NA)], [])
    assert "0.20 USD, cost unknown for 1 of them." in text
    assert "| desktop-only@v1 | #0 | na | not applicable to this harness |" in text
    assert "cost unknown." in render(RUN, [unmetered], [])
