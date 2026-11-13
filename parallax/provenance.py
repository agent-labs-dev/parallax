"""Run provenance: the source revisions and served build behind one run directory, as `provenance.json`.

The runner writes it next to the manifest for every run. Comparisons read the Parallax revision from it;
the harness revision and the served models are the treatment a comparison measures, so they are recorded
here and never required to match (`report.identity_conflicts`). A plugin records its harness revision
through the `parallax.provenance` entry-point group.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from pydantic import Field

from parallax import plugins
from parallax.models import Model, RunManifest, TrialResult

PROVENANCE = "provenance.json"
# Plugins add revision fields: each entry point names a `(run_dir) -> dict[str, str]` callable whose
# keys are recorded as "<entry point name>.<key>" in `Provenance.extra`.
ENTRY_POINT_GROUP = "parallax.provenance"
# A value only the caller knows (`--provenance KEY=VALUE`): the API the run measured, or a field a
# plugin names "<plugin>.<key>", such as the commit that API reported serving.
CALLER_KEY = re.compile(r"^(api_url|[a-z][a-z0-9_-]*\.[a-z][a-z0-9_]*)$")
# The branch-eval runner writes it into a `git archive` tree, which has no `.git` to ask. The runner image
# writes it into its own Parallax tree too, for the same reason.
SOURCE_COMMIT_FILE = ".parallax-source-commit"
PARALLAX_ROOT = Path(__file__).resolve().parent.parent


class Provenance(Model):
    parallax_head: str | None
    task_hashes: dict[str, str]
    judge_policy: str
    api_url: str | None = None
    served_models: tuple[str, ...] = ()
    # Plugin revision fields and caller values, "<plugin>.<key>" -> value.
    extra: dict[str, str] = Field(default_factory=dict, exclude_if=lambda v: not v)


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


def git_head(repo: Path) -> str | None:
    """None outside a git checkout, e.g. an installed wheel."""
    try:
        return _git(repo, "rev-parse", "HEAD").decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def source_commit(worktree: Path) -> str | None:
    """Which commit this tree is: HEAD in a git checkout, else the marker an exported tree carries."""
    if (worktree / ".git").exists() and (head := git_head(worktree)) is not None:
        return head
    marker = worktree / SOURCE_COMMIT_FILE
    return (marker.read_text().strip() or None) if marker.is_file() else None


def installed_commit(name: str) -> str | None:
    """The Git revision recorded by the installer, never an enclosing repository's HEAD."""
    try:
        receipt = distribution(name).read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    if receipt is None:
        return None
    commit = json.loads(receipt).get("vcs_info", {}).get("commit_id")
    return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None


def _listed(repo: Path, *args: str) -> list[bytes]:
    return _git(repo, "ls-files", "-z", *args).split(b"\0")


def sources() -> dict[str, Callable[[Path], Mapping[str, str]]]:
    """The registered provenance callables by entry point name, loaded on use."""
    found: dict[str, Callable[[Path], Mapping[str, str]]] = {}
    for ep in plugins.entry_points_in(ENTRY_POINT_GROUP):
        if ep.name in found:
            raise ValueError(f"provenance source {ep.name!r} is registered twice")
        found[ep.name] = ep.load()
    return found


def plugin_fields(run_dir: Path) -> dict[str, str]:
    merged: dict[str, str] = {}
    for name, source in sources().items():
        for key, value in source(run_dir).items():
            field = f"{name}.{key}"
            if field in merged:
                raise ValueError(f"provenance field {field!r} is recorded twice")
            merged[field] = value
    return merged


def check_caller_keys(extra: Mapping[str, str]) -> None:
    if unknown := sorted(k for k in extra if not CALLER_KEY.match(k)):
        raise ValueError(f"provenance keys must be api_url or <plugin>.<key>, not {unknown}")


def collect(run_dir: Path, extra: Mapping[str, str]) -> Provenance:
    """Read after the manifest is written: the task hashes and grading policy come from it."""
    check_caller_keys(extra)
    manifest = RunManifest.model_validate_json((run_dir / "manifest.json").read_text())
    fields = plugin_fields(run_dir)
    caller = {k: v for k, v in extra.items() if k != "api_url"}
    if both := sorted(caller.keys() & fields.keys()):
        raise ValueError(f"provenance fields {both} are recorded by a plugin and the caller")
    return Provenance(
        parallax_head=source_commit(PARALLAX_ROOT) or installed_commit("parallax-evals"),
        task_hashes=dict(manifest.tasks),
        judge_policy=manifest.grading_policy,
        api_url=extra.get("api_url"),
        extra=fields | caller,
    )


def served_models(results: Iterable[TrialResult]) -> tuple[str, ...]:
    return tuple(sorted({m for r in results for m in r.actual_models}))


def write(run_dir: Path, p: Provenance) -> None:
    (run_dir / PROVENANCE).write_text(p.model_dump_json(indent=2))


def read(run_dir: Path) -> Provenance:
    return Provenance.model_validate_json((run_dir / PROVENANCE).read_text())
