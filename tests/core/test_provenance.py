"""`provenance.json`: the git helpers a plugin's revision fields use, what `collect` records, and how a run dir reads it back."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from parallax import provenance

R1 = Path(__file__).parent / "fixtures" / "runs" / "r1"


def _git(repo: Path, *args: str) -> str:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false")
    return subprocess.run(["git", "-C", str(repo), *identity, *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "harness"
    (repo / "src").mkdir(parents=True)
    (repo / "evals").mkdir()
    (repo / "src" / "agent.py").write_text("PROMPT = 'v1'\n")
    (repo / "evals" / "case.py").write_text("CASE = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    shutil.copy(R1 / "manifest.json", run / "manifest.json")
    return run


def test_the_source_commit_is_head_in_a_checkout_and_the_marker_in_an_exported_tree(
    checkout: Path, tmp_path: Path
) -> None:
    head = _git(checkout, "rev-parse", "HEAD").strip()
    assert provenance.source_commit(checkout) == head == provenance.git_head(checkout)
    exported = tmp_path / "exported"
    shutil.copytree(checkout, exported, ignore=shutil.ignore_patterns(".git"))
    assert provenance.source_commit(exported) is None
    (exported / provenance.SOURCE_COMMIT_FILE).write_text(f"{head}\n")
    assert provenance.source_commit(exported) == head


def test_collect_records_the_run_and_the_caller_values(run_dir: Path) -> None:
    extra = {"api_url": "http://127.0.0.1:8000", "product.served_commit": "abc123"}
    p = provenance.collect(run_dir, extra)
    assert p.task_hashes["saas-a"] == "hash-saas-a"
    assert (p.api_url, p.extra) == ("http://127.0.0.1:8000", {"product.served_commit": "abc123"})
    assert p.parallax_head  # the test runs from a Parallax checkout
    with pytest.raises(ValueError, match="api_url or <plugin>.<key>"):
        provenance.collect(run_dir, {"served_commit": "abc123"})


def test_a_plugin_field_cannot_also_come_from_the_caller(run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provenance, "sources", lambda: {"product": lambda run: {"head": "c1"}})
    with pytest.raises(ValueError, match="recorded by a plugin and the caller"):
        provenance.collect(run_dir, {"product.head": "c2"})


def test_a_run_dir_round_trips_and_one_without_the_file_fails(run_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        provenance.read(run_dir)
    p = provenance.collect(run_dir, {}).model_copy(update={"served_models": ("m1",)})
    provenance.write(run_dir, p)
    assert provenance.read(run_dir) == p


def test_installed_core_never_inherits_an_enclosing_repository_head(checkout, run_dir, monkeypatch, tmp_path):
    from importlib.metadata import PathDistribution

    installed = checkout / ".venv/lib/python3.13/site-packages"
    installed.mkdir(parents=True)
    receipt = tmp_path / "dist-info"
    receipt.mkdir()
    (receipt / "direct_url.json").write_text(json.dumps({"vcs_info": {"commit_id": "a" * 40}}))
    monkeypatch.setattr(provenance, "PARALLAX_ROOT", installed)
    monkeypatch.setattr(provenance, "distribution", lambda name: PathDistribution(receipt), raising=False)
    assert provenance.source_commit(installed) is None
    assert provenance.collect(run_dir, {}).parallax_head == "a" * 40
