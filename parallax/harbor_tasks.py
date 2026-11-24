"""Terminal-Bench 2 (Harbor-format) task importer, source and grading policy.

Upstream tasks are Harbor task directories, not Parallax task directories, so they enter through
`parallax.task_sources` as world-less records like the browser suites. The pinned archive is fetched
at run time, verified against a per-file SHA-256 manifest, and kept in the user cache; no dataset
bytes are committed. Grading is structured, not answer-text matching: the task's own pinned
`tests/test.sh` reports a reward, in the agent's still-live container when the harness reports one
(shared mode), or in a fresh container over the final workspace snapshot. The oracle for a task is
its pinned `solution/solve.sh`, run in the task container before the verifier.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tarfile
import tempfile
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, JsonValue

from parallax.extensions import GradeRequest, GradingPolicy, TaskExtension
from parallax.judging import JudgePolicy
from parallax.models import (
    Area,
    Capability,
    Check,
    FailureMode,
    JsonObject,
    Maturity,
    Model,
    Provenance,
    RubricCheck,
    Task,
    TaskRecord,
)
from parallax.semantic import Graded

TERMINAL_BENCH = "terminal-bench-2"
TB2_POLICY = "terminal-bench-2-upstream-v1"
TERMINAL_BENCH_COMMIT = "2fd12b88aafdd04a52c298e3940bcb189f9766d6"
TERMINAL_BENCH_MANIFEST_SHA256 = "efcb17d74def636fc67695b6a2bfb4a6b65945c2e52e5e0eff05e2f28cf839b3"
TERMINAL_BENCH_EXPECTED_COUNT = 89


@dataclass(frozen=True)
class TerminalBenchSpec:
    source: str
    policy: str
    upstream_commit: str
    archive_url: str
    manifest_sha256: str
    expected_count: int


TERMINAL_BENCH_SPEC = TerminalBenchSpec(
    source=TERMINAL_BENCH,
    policy=TB2_POLICY,
    upstream_commit=TERMINAL_BENCH_COMMIT,
    archive_url=f"https://github.com/harbor-framework/terminal-bench-2/archive/{TERMINAL_BENCH_COMMIT}.tar.gz",
    manifest_sha256=TERMINAL_BENCH_MANIFEST_SHA256,
    expected_count=TERMINAL_BENCH_EXPECTED_COUNT,
)


class HarborTaskError(RuntimeError):
    """A Harbor task directory cannot be imported as a single-step Parallax record."""


class HarborTaskSpec(Model):
    """The typed subset of an upstream Harbor `task.toml` the importer and verifier need."""

    upstream_id: str
    instruction: str
    docker_image: str | None = None
    dockerfile: str | None = None
    network_mode: Literal["no-network", "public", "allowlist"] = "public"
    cpus: float = 2
    memory_mb: int = 4096
    verifier_timeout_s: int = 300
    verifier_mode: Literal["shared", "separate"] = "shared"
    steps: tuple[str, ...] = ()
    tests_digest: str = ""


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "parallax" / "official-suites"


def _tree_digest(root: Path) -> str:
    lines = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}")
    return hashlib.sha256(("\n".join(lines) + ("\n" if lines else "")).encode()).hexdigest()


def tests_digest(tests_dir: Path) -> str:
    """SHA-256 over the sorted per-file digests of a task's pinned `tests/` tree."""
    return _tree_digest(tests_dir)


def _manifest_digest(root: Path) -> str:
    return _tree_digest(root)


def parse_task_dir(task_dir: Path) -> HarborTaskSpec:
    """Parse one Harbor task directory; multi-step tasks are rejected loudly, never truncated."""
    task_dir = Path(task_dir)
    toml_path = task_dir / "task.toml"
    instruction_path = task_dir / "instruction.md"
    if not toml_path.is_file() or not instruction_path.is_file():
        raise HarborTaskError(f"{task_dir}: a Harbor task needs task.toml and instruction.md")
    try:
        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarborTaskError(f"{task_dir}: task.toml cannot be read: {exc}") from exc
    steps = raw.get("steps") or []
    if steps:
        raise HarborTaskError(f"{task_dir}: multi-step Harbor tasks are not supported by this importer")
    instruction = instruction_path.read_text(encoding="utf-8").strip()
    if not instruction:
        raise HarborTaskError(f"{task_dir}: instruction.md is empty")

    def table(key: str) -> dict[str, Any]:
        value = raw.get(key)
        return value if isinstance(value, dict) else {}

    task_table = table("task")
    name = str(task_table.get("name") or task_dir.name)
    upstream_id = name.rsplit("/", 1)[-1].strip() or task_dir.name
    environment = table("environment")
    verifier = table("verifier")
    network = environment.get("network_mode")
    if network is None:
        network = "public" if environment.get("allow_internet", True) else "no-network"
    if network not in {"no-network", "public", "allowlist"}:
        raise HarborTaskError(f"{task_dir}: unsupported network_mode {network!r}")
    dockerfile = "environment/Dockerfile" if (task_dir / "environment" / "Dockerfile").is_file() else None
    tests_dir = task_dir / "tests"
    if not (tests_dir / "test.sh").is_file():
        raise HarborTaskError(f"{task_dir}: missing tests/test.sh, the upstream verifier entry point")
    if not (task_dir / "solution" / "solve.sh").is_file():
        raise HarborTaskError(f"{task_dir}: missing solution/solve.sh, the upstream oracle")
    return HarborTaskSpec(
        upstream_id=upstream_id,
        instruction=instruction,
        docker_image=environment.get("docker_image"),
        dockerfile=dockerfile,
        network_mode=cast(Literal["no-network", "public", "allowlist"], network),
        cpus=float(environment.get("cpus", 2)),
        memory_mb=int(environment.get("memory_mb", 4096)),
        verifier_timeout_s=int(float(verifier.get("timeout_sec", 300))),
        tests_digest=tests_digest(tests_dir),
    )


def _task_id(upstream_id: str, used: set[str]) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", upstream_id.lower()).strip("-") or "task"
    candidate = f"{TERMINAL_BENCH}-{stem}"
    if len(candidate) > 64:
        digest = hashlib.sha256(upstream_id.encode()).hexdigest()[:10]
        candidate = f"{candidate[:53].rstrip('-')}-{digest}"
    if candidate in used:
        digest = hashlib.sha256(upstream_id.encode()).hexdigest()[:10]
        candidate = f"{candidate[:53].rstrip('-')}-{digest}"
    used.add(candidate)
    return candidate


def _record(spec: HarborTaskSpec, task_dir: Path, used: set[str]) -> TaskRecord:
    data: JsonObject = {
        "upstream_id": spec.upstream_id,
        "task_dir": str(task_dir),
        "docker_image": spec.docker_image,
        "dockerfile": spec.dockerfile,
        "network_mode": spec.network_mode,
        "cpus": spec.cpus,
        "memory_mb": spec.memory_mb,
        "verifier_timeout_s": spec.verifier_timeout_s,
        "tests_digest": spec.tests_digest,
    }
    return TaskRecord(
        id=_task_id(spec.upstream_id, used),
        version=1,
        area=Area.CODE,
        summary=f"{TERMINAL_BENCH} task {spec.upstream_id}",
        prompt=spec.instruction,
        grading=TB2_POLICY,
        requires=frozenset({Capability.WORKSPACE, Capability.SHELL}),
        maturity=Maturity.REGRESSION,
        provenance=Provenance.CONVERTED,
        data=data,
    )


def records_from_root(root: Path) -> list[TaskRecord]:
    task_dirs = sorted(p for p in Path(root).iterdir() if p.is_dir() and (p / "task.toml").is_file())
    used: set[str] = set()
    return [_record(parse_task_dir(path), path, used) for path in task_dirs]


def _extract_root(archive: Path, cache: Path) -> Path:
    destination = cache / f"{TERMINAL_BENCH}-{TERMINAL_BENCH_COMMIT}-{TERMINAL_BENCH_MANIFEST_SHA256}"
    if destination.is_dir():
        if _manifest_digest(destination) == TERMINAL_BENCH_MANIFEST_SHA256:
            return destination
        raise HarborTaskError(f"{TERMINAL_BENCH}: cached task tree failed its manifest digest")
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{TERMINAL_BENCH}-", dir=cache) as temporary:
        staging = Path(temporary) / "tree"
        staging.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                target = (staging / member.name).resolve()
                if not target.is_relative_to(staging.resolve()):
                    raise HarborTaskError(f"{TERMINAL_BENCH}: archive member escapes its root: {member.name}")
            bundle.extractall(staging, filter="data")
        children = [p for p in staging.iterdir() if p.is_dir()]
        if len(children) != 1:
            raise HarborTaskError(f"{TERMINAL_BENCH}: archive does not hold one task tree")
        tree = children[0]
        if _manifest_digest(tree) != TERMINAL_BENCH_MANIFEST_SHA256:
            raise HarborTaskError(f"{TERMINAL_BENCH}: task tree failed its manifest digest")
        os.replace(tree, destination)
    return destination


def fetch_terminal_bench(*, cache: Path | None = None, transport: httpx.BaseTransport | None = None) -> Path:
    """Download the pinned TB2 archive on first use and return its verified task tree."""
    root = cache or cache_root()
    destination = root / f"{TERMINAL_BENCH}-{TERMINAL_BENCH_COMMIT}-{TERMINAL_BENCH_MANIFEST_SHA256}"
    if destination.is_dir() and _manifest_digest(destination) == TERMINAL_BENCH_MANIFEST_SHA256:
        return destination
    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"{TERMINAL_BENCH}-{TERMINAL_BENCH_COMMIT}.tar.gz"
    if not archive.is_file():
        try:
            with httpx.Client(transport=transport, follow_redirects=True, timeout=120.0) as client:
                response = client.get(TERMINAL_BENCH_SPEC.archive_url)
                response.raise_for_status()
                data = response.content
        except httpx.HTTPError as exc:
            raise HarborTaskError(f"{TERMINAL_BENCH}: dataset download failed: {exc}") from exc
        fd, temporary = tempfile.mkstemp(prefix=f".{archive.name}.", dir=root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, archive)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    return _extract_root(archive, root)


def terminal_bench_tasks(root: Path | None = None) -> Iterable[TaskRecord]:
    tree = Path(root) if root is not None else fetch_terminal_bench()
    records = records_from_root(tree)
    if root is None and len(records) != TERMINAL_BENCH_EXPECTED_COUNT:
        raise HarborTaskError(f"{TERMINAL_BENCH}: expected {TERMINAL_BENCH_EXPECTED_COUNT} tasks, got {len(records)}")
    return records


def oracle_path(data: JsonObject) -> Path:
    """The task's pinned oracle script: run it in the task container, then verify."""
    return Path(str(data["task_dir"])) / "solution" / "solve.sh"


# ---------------------------------------------------------------------------
# grading: the task's own tests/test.sh, reward 1 is a pass
# ---------------------------------------------------------------------------


def _checks(_task: Task) -> tuple[Check, ...]:
    return (
        RubricCheck(
            id="upstream_verifier",
            kind="rubric",
            mode=FailureMode.WRONG_OUTPUT,
            items=("The pinned task's own tests/test.sh reports reward 1 in the task environment.",),
        ),
    )


def _result(check: Check, passed: bool | None, detail: str) -> Any:
    from parallax.models import CheckResult

    return CheckResult(
        id=check.id, kind=check.kind, mode=check.mode, gate=check.gate, advisory=False,
        weight=check.weight, passed=passed, detail=detail,
    )  # fmt: skip


def _unjudged(task: Task, detail: str) -> Graded:
    return Graded(checks=tuple(_result(check, None, detail) for check in _checks(task)))


def _graded(task: Task, reward: float, mode: str) -> Graded:
    detail = f"upstream verifier reward={reward:.6g}; mode={mode}; revision={TERMINAL_BENCH_COMMIT}"
    return Graded(
        checks=tuple(_result(check, reward == 1.0, detail) for check in _checks(task)),
        judge_identity=f"{TB2_POLICY}:{TERMINAL_BENCH}:verifier-{TERMINAL_BENCH_COMMIT}",
        score=reward,
    )


async def _snapshot_verify(task: Task, workspace: Path) -> dict[str, JsonValue]:
    """Run the pinned `tests/test.sh` in a fresh `--network none` container from the task image, with the
    final workspace snapshot copied into the image workdir and the pinned tests mounted at `/tests`.
    Snapshot-unsafe tasks (tests reading live container state outside the workspace) are graded through
    the shared-mode harness evidence instead; this path never runs in the agent's container."""
    data = task.data
    image = data.get("docker_image")
    task_dir = data.get("task_dir")
    if not isinstance(image, str) or not image or not isinstance(task_dir, str):
        raise HarborTaskError("snapshot verify needs the task docker_image and task_dir")
    tests = Path(task_dir) / "tests"
    timeout_value = data.get("verifier_timeout_s")
    timeout = int(timeout_value) if isinstance(timeout_value, (int, float)) else 300
    argv = [
        "docker", "run", "--rm", "--network", "none", "--security-opt", "no-new-privileges",
        "-v", f"{Path(workspace).resolve()}:/snapshot:ro",
        "-v", f"{tests.resolve()}:/tests:ro",
        image, "sh", "-c",
        "cp -R /snapshot/. ./ 2>/dev/null; mkdir -p /logs/verifier; bash /tests/test.sh; cat /logs/verifier/reward.txt 2>/dev/null",
    ]  # fmt: skip
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return {"reward": 0.0, "exit": None, "mode": "snapshot", "detail": f"verifier timed out after {timeout}s"}
    lines = out.decode(errors="replace").strip().splitlines()
    reward: float | None = None
    for line in reversed(lines):
        try:
            reward = float(line.strip())
            break
        except ValueError:
            continue
    if reward is None:
        raise HarborTaskError(f"verifier produced no reward file: {err.decode(errors='replace')[-300:]}")
    return {"reward": reward, "exit": process.returncode, "mode": "snapshot"}


async def _grade(req: GradeRequest) -> Graded:
    interruption = req.execution.get("error")
    if isinstance(interruption, str) and interruption.startswith("environment interruption: "):
        return _unjudged(req.task, interruption)
    product = req.product or {}
    shared = product.get("terminal_bench")
    if isinstance(shared, dict) and isinstance(shared.get("reward"), (int, float)):
        reward = float(cast(float, shared["reward"]))
        if not 0 <= reward <= 1:
            return _unjudged(req.task, "missing evidence: upstream verifier returned an out-of-range reward")
        return _graded(req.task, reward, str(shared.get("mode") or "shared"))
    workspace = getattr(req.evidence, "workspace", None) if req.evidence is not None else None
    if workspace is not None:
        try:
            outcome = await _snapshot_verify(req.task, Path(workspace))
        except (HarborTaskError, OSError) as exc:
            return _unjudged(req.task, f"missing evidence: upstream verifier unavailable: {exc}")
        reward = float(cast(float, outcome["reward"]))
        return _graded(req.task, reward, str(outcome.get("mode") or "snapshot"))
    return _unjudged(req.task, "missing evidence: no shared-container verifier reward or workspace snapshot")


def _no_world(_task: Task, _evidence: Any, _product: JsonObject | None) -> dict[str, JsonValue]:
    from parallax.judge import MissingEvidence

    raise MissingEvidence("terminal-bench-2 is graded by its upstream verifier, not world evidence")


TB2_POLICY_OBJECT = GradingPolicy(
    id=TB2_POLICY,
    checks=_checks,
    evidence=_no_world,
    grade=_grade,
    judge=JudgePolicy(
        name=f"{TB2_POLICY}:{TERMINAL_BENCH_COMMIT}",
        questions={"upstream": "The pinned upstream verifier decides the task."},
        evidence_form=f"terminal-bench-2 tests/test.sh reward, pinned revision {TERMINAL_BENCH_COMMIT}",
    ),
    identity=lambda: f"{TB2_POLICY}:{TERMINAL_BENCH}:verifier-{TERMINAL_BENCH_COMMIT}",
)


def _extension_model() -> type[BaseModel]:
    from parallax.official_suites import OfficialMetadata

    return OfficialMetadata


TB2_POLICY_EXTENSION = TaskExtension(
    name="official-terminal-bench-2",
    model=_extension_model(),
    default_services=False,
    policy=TB2_POLICY_OBJECT,
)

__all__ = [
    "TB2_POLICY",
    "TB2_POLICY_EXTENSION",
    "TERMINAL_BENCH",
    "TERMINAL_BENCH_COMMIT",
    "TERMINAL_BENCH_EXPECTED_COUNT",
    "TERMINAL_BENCH_MANIFEST_SHA256",
    "TERMINAL_BENCH_SPEC",
    "HarborTaskError",
    "HarborTaskSpec",
    "fetch_terminal_bench",
    "oracle_path",
    "parse_task_dir",
    "records_from_root",
    "terminal_bench_tasks",
    "tests_digest",
]
