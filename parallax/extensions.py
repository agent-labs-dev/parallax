"""Task extensions: plugin-owned `[metadata.<name>]` blocks in task.toml and the lane rules they impose.

A plugin registers a `TaskExtension` under the `parallax.task_extensions` entry-point group. Core
validates the block with the extension's model and enforces its lane rules without knowing the lane.
Kept dependency-light: it must be importable from models, task and runner.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, JsonValue

from parallax import plugins

if TYPE_CHECKING:
    import httpx

    from parallax import jev
    from parallax.judge import JudgeConfig, JudgePanel
    from parallax.judging import JudgePolicy
    from parallax.models import Check, Evidence, JsonObject, Task
    from parallax.semantic import Graded
    from parallax.track.rows import LoadedRun

ENTRY_POINT_GROUP = "parallax.task_extensions"
RUN_ARTIFACTS_GROUP = "parallax.run_artifacts"
RESERVED = "parallax"  # the core `[metadata.parallax]` table


@dataclass(frozen=True)
class LaneRules:
    """What a run must look like for a task in this lane; `label` names the lane in the error."""

    label: str
    harnesses: tuple[str, ...] | None = None  # None: any harness may run the task
    concurrency_one: bool = False

    def violated(self, harnesses: Sequence[str], concurrency: int) -> bool:
        if self.harnesses is not None and (
            not harnesses or len(set(harnesses)) != len(harnesses) or any(h not in self.harnesses for h in harnesses)
        ):
            return True
        return self.concurrency_one and concurrency != 1

    def message(self) -> str:
        parts = [
            *([f"only {' or '.join(self.harnesses)}"] if self.harnesses is not None else []),
            *(["--concurrency 1"] if self.concurrency_one else []),
        ]
        joined = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
        return f"{self.label} collection requires {joined}"


@dataclass(frozen=True)
class GradeRequest:
    """Everything a policy's own `grade` may read of one trial; nothing here is a secret."""

    task: Task
    evidence: Evidence | None  # None for a world-less task
    answers: tuple[str, ...]  # one per delivered turn
    product: JsonObject | None  # the harness's `trial_metadata`, None when it reported none
    execution: dict[str, JsonValue]  # status, error, notes and tool diagnostics, as the validity judge reads them
    harness_model: str | None
    panel: JudgePanel | None
    judge_config: JudgeConfig | None
    jev_config: jev.JevConfig | None
    client: httpx.AsyncClient


@dataclass(frozen=True)
class GradingPolicy:
    """Grades the extension's tasks from product state its harness reported, not the world alone."""

    id: str  # versioned and part of the run's grading policy: change a question, change the id
    checks: Callable[[Task], Sequence[Check]]  # replaces the task's checks; each becomes one question
    # The judge's evidence from the world evidence and the harness's `trial_metadata` (None when it
    # reported none); raises `judge.MissingEvidence` when they cannot support a grade.
    evidence: Callable[[Task, Evidence, JsonObject | None], dict[str, JsonValue]]
    # The policy grades the trial itself instead of the core judge flow over `evidence`: required for a
    # world-less task, which has no world evidence. It returns one result per check in `checks`.
    grade: Callable[[GradeRequest], Awaitable[Graded]] | None = None
    # What its judge uses, when the policy declares it: its identity is recorded on every row it grades.
    judge: JudgePolicy | None = None


@dataclass(frozen=True)
class TaskExtension:
    name: str  # the task.toml namespace: `[metadata.<name>]`
    model: type[BaseModel]  # validates that block
    lane: LaneRules | None = None
    # The block is a sub-table of `[metadata.parallax]` instead: tasks written before plugin namespaces
    # existed keep their task.toml, and so their content hash and saved judgments, unchanged.
    core_table: bool = False
    # Rules across the loaded task (its area, steps, faults, other files); each message rejects the task.
    check: Callable[[Task], Iterable[str]] | None = None
    # Validate missing declarations too, for extensions that require metadata for an area or file.
    check_absent: bool = False
    # False when the block replaces the synthetic world, so an MCP task mounts no default services.
    default_services: bool = True
    # True when the block's task acts on real accounts it owns, so its addresses are real by design and
    # exempt from the synthetic-address lint. The secret lint still applies.
    real_accounts: bool = False
    policy: GradingPolicy | None = None


@functools.cache
def task_extensions() -> Mapping[str, TaskExtension]:
    """Every registered extension by namespace, loaded on first use."""
    found: dict[str, TaskExtension] = {}
    for ep in plugins.entry_points_in(ENTRY_POINT_GROUP):
        ext = ep.load()
        if not isinstance(ext, TaskExtension):
            raise TypeError(f"task extension {ep.value!r} is not a TaskExtension")
        if ext.name != ep.name or ext.name == RESERVED or ext.name in found:
            raise ValueError(f"task extension {ep.value!r} cannot claim namespace {ext.name!r}")
        found[ext.name] = ext
    return found


def core_tables() -> frozenset[str]:
    """The `[metadata.parallax]` keys an extension claims instead of core."""
    return frozenset(name for name, ext in task_extensions().items() if ext.core_table)


def validate_blocks(blocks: Mapping[str, object]) -> dict[str, JsonValue]:
    """Validate each plugin metadata table by its extension; an unregistered namespace is an error."""
    if not blocks:
        return {}
    registry = task_extensions()
    out: dict[str, JsonValue] = {}
    for ns, block in blocks.items():
        ext = registry.get(ns)
        if ext is None:
            raise ValueError(f"unknown task metadata namespace {ns!r}; registered: {sorted(registry)}")
        out[ns] = ext.model.model_validate(block).model_dump(mode="json")
    return out


def check(task: Task) -> None:
    """Raise for the first rule an extension's check finds the loaded task breaks."""
    for ns, ext in task_extensions().items():
        if ns not in task.extensions and not ext.check_absent:
            continue
        if (rule := ext.check) is not None and (problems := list(rule(task))):
            raise ValueError(f"metadata.{ns}: {problems[0]}")


def default_services(task: Task) -> bool:
    return all(task_extensions()[ns].default_services for ns in task.extensions)


def real_accounts(task: Task) -> bool:
    return any(task_extensions()[ns].real_accounts for ns in task.extensions)


@dataclass(frozen=True)
class RunArtifact:
    """A file a plugin adds to every finished run it can describe, such as a campaign ledger or a summary table.

    `build` reads the run back and returns the file's bytes. It must be deterministic: the file is written into the run
    directory when the run is sealed, so the seal hashes it, every item's evidence zip carries it, and the upload
    rebuilds it and refuses a run whose rebuild differs from what was sealed. `path` is relative to the run directory,
    outside `trials/`, and never one of Parallax's own files.
    """

    path: str
    build: Callable[[LoadedRun], bytes]


def run_artifacts() -> tuple[RunArtifact, ...]:
    """Every `parallax.run_artifacts` entry point's artifact, by entry point name; paths must be distinct."""
    found: list[RunArtifact] = []
    for ep in plugins.entry_points_in(RUN_ARTIFACTS_GROUP):
        artifact = ep.load()
        if not isinstance(artifact, RunArtifact):
            raise TypeError(f"run artifact {ep.value!r} is a {type(artifact).__name__}, not a RunArtifact")
        found.append(artifact)
    paths = [a.path for a in found]
    if len(set(paths)) != len(paths):
        raise ValueError(f"two run artifacts share a path: {sorted(paths)}")
    return tuple(found)


@functools.cache
def grading_policies() -> Mapping[str, GradingPolicy]:
    """Every grading policy a task source's record may name: Parallax's own and each extension's."""
    from parallax.browser_evidence import BROWSER_EVIDENCE  # lazy: it imports the judge, which imports models

    found: dict[str, GradingPolicy] = {BROWSER_EVIDENCE.id: BROWSER_EVIDENCE}
    for ext in task_extensions().values():
        if ext.policy is not None:
            if ext.policy.id in found and found[ext.policy.id] is not ext.policy:
                raise ValueError(f"grading policy {ext.policy.id!r} is registered twice")
            found[ext.policy.id] = ext.policy
    return found


def grading_policy(task: Task) -> GradingPolicy | None:
    found = [p for ns in task.extensions if (p := task_extensions()[ns].policy) is not None]
    if task.grading is not None:
        policy = grading_policies().get(task.grading)
        if policy is None:
            raise ValueError(
                f"task {task.meta.id} names grading policy {task.grading!r}; known: {sorted(grading_policies())}"
            )
        found.append(policy)
    if len(found) > 1:
        raise ValueError(f"task {task.meta.id} has {len(found)} grading policies; an extension owns at most one")
    return found[0] if found else None


def check_lanes(tasks: Iterable[Task], harnesses: Sequence[str], concurrency: int) -> None:
    """Raise for the first lane, by label, whose rules this run breaks."""
    lanes = (task_extensions()[ns].lane for t in tasks for ns in t.extensions)
    rules = dict.fromkeys(lane for lane in lanes if lane is not None)
    for rule in sorted(rules, key=lambda r: r.label):
        if rule.violated(tuple(harnesses), concurrency):
            raise ValueError(rule.message())
