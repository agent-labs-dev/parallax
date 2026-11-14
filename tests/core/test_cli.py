"""CLI exit codes, the Langfuse preflight, and the workflow `parallax ci init` writes."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from parallax import cli, runner
from parallax.cli import ENVIRONMENT, FAILED, OK, USAGE, main
from parallax.track import sink, upload
from parallax.track.compare import RunRecord

TASKS = str(Path(__file__).parent / "fixtures" / "tasks")
RUN = ["run", TASKS, "--harness", "oracle", "--isolation", "host"]


def _tracked(name: str, run_id: str, row: str = "h|m", branch: str = "main") -> RunRecord:
    return RunRecord(
        name,
        f"{run_id}-{row}",
        datetime(2026, 11, 1, tzinfo=UTC),
        {"run_id": run_id, "row": row, "branch": branch},
        {},
        {},
    )


def test_baseline_accepts_a_run_reference_beside_metadata_filters() -> None:
    run = _tracked("gh-42050950143-1", "gh-42050950143-1")
    where, named = cli.baseline_args(["branch=main", "gh-42050950143-1", "note=x=y"], [run])
    assert where == {"branch": "main", "note": "x=y"}
    assert named == [run]


def test_baseline_prefers_an_exact_run_reference_over_metadata_with_an_equals_sign() -> None:
    run = _tracked("release=old", "release=old")
    where, named = cli.baseline_args(["release=old"], [run])
    assert where == {}
    assert named == [run]


def test_baseline_rejects_a_reference_to_no_tracked_run() -> None:
    with pytest.raises(cli.UsageError, match="matched no run"):
        cli.baseline_args(["gh-missing"], [])


@pytest.mark.parametrize("value", ["branch=", "=main"])
def test_baseline_rejects_an_empty_metadata_key_or_value(value: str) -> None:
    with pytest.raises(cli.UsageError):
        cli.baseline_args([value], [])


def test_a_run_without_langfuse_stops_before_executing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LANGFUSE_BASE_URL", "LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runner, "run", lambda *args: pytest.fail("execution started without Langfuse"))
    assert main([*RUN, "--out", str(tmp_path / "run")]) == ENVIRONMENT


@pytest.mark.parametrize(("uploaded", "want"), [(OK, FAILED), (ENVIRONMENT, ENVIRONMENT)])
def test_an_invalid_run_is_sealed_uploaded_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uploaded: int, want: int
) -> None:
    sealed: list[dict[str, str]] = []
    monkeypatch.setattr(sink, "connect", lambda write: (None, None))
    monkeypatch.setattr(runner, "run", lambda *args: tmp_path)
    monkeypatch.setattr(runner, "latest_results", lambda *args: [])
    monkeypatch.setattr(runner, "invalid_share", lambda *args: 1.0)
    monkeypatch.setattr(upload, "seal", lambda run_dir, *, dataset, note, meta: sealed.append(meta))
    monkeypatch.setattr(cli, "upload_one", lambda run_dir: uploaded)
    assert main([*RUN, "--meta", "branch=main"]) == want
    assert sealed == [{"branch": "main"}]


def test_nothing_pending_is_not_an_error(tmp_path: Path) -> None:
    assert main(["upload", "--resume", "--root", str(tmp_path)]) == OK


@pytest.mark.parametrize(
    "argv",
    [
        ["check", "no-such-task", "--tasks", TASKS, "--lint-only"],
        [*RUN, "--world-port", "17801"],
        [*RUN, "--provenance", "no-equals-sign"],
        *([*RUN, "--pair", pair, "--dry-run"] for pair in ("mini-repo", "=m/x", "mini-repo=")),
        *(["ci", "init", "--suite", "core", "--harness", "oracle", "--runner", r] for r in ("a: b", "a'b")),
        ["ci", "init", "--suite", "core", "--harness", "oracle", "--upstream", "gemini"],
        ["ci", "init", "--suite", "core", "--harness", "oracle", "--report-sink", "slack"],
    ],
)
def test_bad_invocations_are_usage_errors(argv: list[str]) -> None:
    assert main(argv) == USAGE


def test_resume_of_a_non_run_is_a_usage_error(tmp_path: Path) -> None:
    assert main([*RUN, "--resume", str(tmp_path)]) == USAGE


def test_check_reports_unjudged_controls(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", TASKS, "--isolation", "host"]) == FAILED
    assert "0/3 tasks pass their controls" in capsys.readouterr().out


def test_show_says_which_experiments_it_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = sink.Listing((), 2, ("run_id", "row"))
    monkeypatch.setattr(sink, "connect", lambda write: (None, None))
    monkeypatch.setattr(sink, "list_runs", lambda client, dataset: listing)
    assert main(["show"]) == OK
    assert capsys.readouterr().out.strip() == (
        "2 experiments on this dataset lack Parallax run metadata (run_id, row); they were skipped"
    )


def test_compare_names_the_metadata_a_skipped_experiment_lacks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listing = sink.Listing((), 6, ("run_id", "judge_policy"))
    monkeypatch.setattr(sink, "connect", lambda write: (None, None))
    monkeypatch.setattr(sink, "list_runs", lambda client, dataset: listing)
    assert main(["compare"]) == ENVIRONMENT
    err = capsys.readouterr().err
    assert "nothing to compare: 6 experiments on this dataset lack Parallax run metadata" in err
    assert "nothing is recorded yet" not in err


def test_compare_still_says_nothing_is_recorded_for_an_empty_dataset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sink, "connect", lambda write: (None, None))
    monkeypatch.setattr(sink, "list_runs", lambda client, dataset: sink.Listing(()))
    assert main(["compare"]) == ENVIRONMENT
    assert "nothing is recorded yet" in capsys.readouterr().err


def test_ci_init_writes_a_workflow_that_runs_compares_and_comments(tmp_path: Path) -> None:
    path = tmp_path / "parallax.yml"
    argv = ["ci", "init", "--suite", "core", "--harness", "hermes", "--model", "openai/gpt-5", "--path", str(path)]
    install = 'echo "step: one" && pip install hermes-agent  # pinned\nnpm i -g other'
    assert main([*argv, "--install", install]) == OK
    workflow = yaml.safe_load(path.read_text())
    job = workflow["jobs"]["evaluate"]
    assert job["if"].startswith("vars.PARALLAX_LANGFUSE_PROJECT != ''") and "head.repo.fork" in job["if"]
    # A push or pull request runs only once the repository sets a campaign budget.
    assert "vars.PARALLAX_BUDGET_USD != ''" in job["if"] and "vars.PARALLAX_RESERVE_USD != ''" in job["if"]
    assert {"budget", "reserve"} <= {
        k for k, v in workflow[True]["workflow_dispatch"]["inputs"].items() if v.get("required")
    }
    # Without a fallback panel every question Jev leaves uncertain makes the trial INVALID.
    assert {"PARALLAX_JUDGE_MODELS", "PARALLAX_JUDGE_BASE_URL", "PARALLAX_JUDGE_API_KEY"} <= job["env"].keys()
    # Both metered wires and both extra report sinks are written unless the options narrow them.
    assert {"PARALLAX_UPSTREAM_OPENAI_KEY", "PARALLAX_UPSTREAM_ANTHROPIC_KEY"} <= job["env"].keys()
    steps = {step.get("name"): step for step in job["steps"]}
    assert {"Post to the results issue", "Send the report to a webhook"} <= steps.keys()
    assert "inputs.runner" in job["runs-on"] and "github.run_id" in workflow["concurrency"]["group"]
    assert steps["Install the harness"]["run"] == install + "\n"
    assert steps["Finish an interrupted upload"]["if"] == "failure()"
    comment = steps["Compare with main"]["run"]
    assert 'compare "$RUN_ID" --baseline "branch=main"' in comment and "gh pr comment" in comment
    assert "--edit-last" not in comment  # it would edit another workflow's newer comment
    assert main(argv) == USAGE  # an existing workflow is kept unless --force
    assert main([*argv, "--force"]) == OK


@pytest.mark.parametrize("wire", ["openai", "anthropic"])
def test_ci_init_names_only_the_upstream_wire_it_is_given(tmp_path: Path, wire: str) -> None:
    path = tmp_path / "parallax.yml"
    argv = ["ci", "init", "--suite", "lite", "--harness", "hermes", "--model", "openai/gpt-5"]
    assert main([*argv, "--upstream", wire, "--report-sink", "issue", "--path", str(path)]) == OK
    workflow = yaml.safe_load(path.read_text())
    job = workflow["jobs"]["evaluate"]
    text = path.read_text()
    other = "ANTHROPIC" if wire == "openai" else "OPENAI"
    assert f"PARALLAX_UPSTREAM_{wire.upper()}_KEY" in job["env"]
    assert f"PARALLAX_UPSTREAM_{other}_KEY" not in text
    names = {step.get("name") for step in job["steps"]}
    assert "Post to the results issue" in names and "Send the report to a webhook" not in names
    assert "PARALLAX_REPORT_WEBHOOK" not in text
    assert job["permissions"]["issues"] == "write"


def test_ci_init_writes_no_provider_key_or_report_sink_when_given_none(tmp_path: Path) -> None:
    path = tmp_path / "parallax.yml"
    argv = ["ci", "init", "--suite", "conformance", "--harness", "oracle"]
    assert main([*argv, "--upstream", "", "--report-sink", "", "--path", str(path)]) == OK
    workflow = yaml.safe_load(path.read_text())
    job = workflow["jobs"]["evaluate"]
    text = path.read_text()
    assert not [key for key in job["env"] if key.startswith("PARALLAX_UPSTREAM_")]
    assert "PARALLAX_UPSTREAM_" not in text and "PARALLAX_REPORT_WEBHOOK" not in text
    names = {step.get("name") for step in job["steps"]}
    assert "Post to the results issue" not in names and "Send the report to a webhook" not in names
    assert "issues" not in job["permissions"]


def evaluate(script: str, tmp_path: Path, **env: str) -> list[str]:
    """The Evaluate step as the runner executes it, with `parallax` replaced by a stub that prints its argv."""
    stub = tmp_path / "parallax"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    stub.chmod(0o755)
    base = {
        "PATH": os.environ["PATH"],
        "PARALLAX": str(stub),
        "BRANCH": "main",
        "RUN_ID": "gh-1-1",
        "GITHUB_SHA": "a" * 40,
    }
    done = subprocess.run(
        ["bash", "-e", "-c", script], env=base | env, cwd=tmp_path, capture_output=True, text=True, check=True
    )
    return done.stdout.splitlines()


def test_a_dispatch_overrides_the_defaults_and_records_no_baseline(tmp_path: Path) -> None:
    path = tmp_path / "parallax.yml"
    argv = ["ci", "init", "--suite", "core conf-*", "--harness", "oracle", "--repeats", "2", "--path", str(path)]
    assert main(argv) == OK
    (tmp_path / "conf-x").touch()  # a glob the shell must not expand
    script = next(
        s["run"] for s in yaml.safe_load(path.read_text())["jobs"]["evaluate"]["steps"] if s.get("name") == "Evaluate"
    )
    push = evaluate(
        script, tmp_path, GITHUB_EVENT_NAME="push", SUITE="", HARNESS="", MODEL="", BUDGET="5", RESERVE="0.5"
    )
    assert push[push.index("--budget") + 1] == "5" and push[push.index("--reserve") + 1] == "0.5"
    with pytest.raises(subprocess.CalledProcessError, match="returned non-zero"):
        evaluate(script, tmp_path, GITHUB_EVENT_NAME="push", SUITE="", HARNESS="", MODEL="", BUDGET="", RESERVE="0.5")
    assert push[:6] == ["run", "core", "conf-*", "--harness", "oracle", "--repeats"] and "branch=main" in push
    assert "--model" not in push and push[push.index("--note") + 1] == "main aaaaaaaa"
    dispatch = evaluate(
        script, tmp_path, GITHUB_EVENT_NAME="workflow_dispatch", SUITE="lite", HARNESS="hermes", MODEL="openai/gpt-5-mini",
        REPEATS="1", CONCURRENCY="8", NOTE="scale run B", BUDGET="20", RESERVE="1",
    )  # fmt: skip
    assert dispatch[:6] == ["run", "lite", "--harness", "hermes", "--model", "openai/gpt-5-mini"]
    assert (
        dispatch[dispatch.index("--concurrency") + 1] == "8" and dispatch[dispatch.index("--note") + 1] == "scale run B"
    )
    assert "branch=main" not in dispatch  # a campaign never becomes the branch's baseline
