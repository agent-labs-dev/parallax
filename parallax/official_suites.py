"""Task sources and upstream grading policies for the public browser suites.

The datasets are downloaded only when their source is selected. They are verified before use and are
stored below the user's cache, never in the repository. The upstream judge runs in its prepared checkout
so its prompts and scoring code remain owned by that project.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, JsonValue

from parallax.browser_evidence import reported
from parallax.extensions import GradeRequest, GradingPolicy, TaskExtension
from parallax.judge import MissingEvidence
from parallax.judging import JudgePolicy
from parallax.models import (
    Area,
    Capability,
    Check,
    CheckResult,
    Evidence,
    FailureMode,
    JsonObject,
    Model,
    Provenance,
    RubricCheck,
    Task,
    TaskRecord,
)
from parallax.semantic import Graded

BU_BENCH = "bu-bench"
ONLINE_MIND2WEB = "online-mind2web"
BU_POLICY = "bu-bench-upstream-v1"
MIND2WEB_POLICY = "online-mind2web-upstream-v1"
BU_UPSTREAM_COMMIT = "77b4df339fe8fc293fb48896cfa1c73dd743af42"
BU_DATASET_SHA256 = "0c014b056192e261b06f04ed6da6dcf37f2135150cf58c87ea839bd4e150e5c3"
MIND2WEB_UPSTREAM_COMMIT = "b422075b059f0bba670ddda168444eb1d326aa45"
MIND2WEB_DATASET_REVISION = "eacad896a84dc5b65e29b0b06e4699ab0544d701"
MIND2WEB_DATASET_SHA256 = "7dedf381531d423dc0fc48d21dc1d425655dd75f38b10211a880fa09102f257f"
MIND2WEB_DATASET_GIT_BLOB = "e8a5e5a99f2be9eae14f4e4259bd5af562f80da9"
BU_JUDGE_MODEL = "gpt-5.6-luna"


@dataclass(frozen=True)
class SuiteSpec:
    source: str
    policy: str
    upstream_commit: str
    dataset_url: str
    dataset_sha256: str
    dataset_revision: str
    expected_count: int
    judge_model: str | None = None
    git_blob_sha256: str | None = None
    decrypt_name: str | None = None


BU_SPEC = SuiteSpec(
    source=BU_BENCH,
    policy=BU_POLICY,
    upstream_commit=BU_UPSTREAM_COMMIT,
    dataset_url=(f"https://raw.githubusercontent.com/browser-use/benchmark/{BU_UPSTREAM_COMMIT}/BU_Bench_V2.enc"),
    dataset_sha256=BU_DATASET_SHA256,
    dataset_revision=BU_UPSTREAM_COMMIT,
    expected_count=200,
    judge_model=BU_JUDGE_MODEL,
    decrypt_name="BU_Bench_V2",
)
MIND2WEB_SPEC = SuiteSpec(
    source=ONLINE_MIND2WEB,
    policy=MIND2WEB_POLICY,
    upstream_commit=MIND2WEB_UPSTREAM_COMMIT,
    dataset_url=(
        "https://huggingface.co/datasets/osunlp/Online-Mind2Web/resolve/"
        f"{MIND2WEB_DATASET_REVISION}/Online_Mind2Web.json"
    ),
    dataset_sha256=MIND2WEB_DATASET_SHA256,
    dataset_revision=MIND2WEB_DATASET_REVISION,
    expected_count=300,
    git_blob_sha256=MIND2WEB_DATASET_GIT_BLOB,
)


class OfficialSuiteError(RuntimeError):
    """A pinned source or its prepared judge cannot be used."""


class OfficialMetadata(BaseModel):
    """The source owns all task data; the extension namespace is intentionally empty."""


class _Config(Model):
    root: Path
    python: Path
    base_url: str
    api_key: str


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "parallax" / "official-suites"


def _cache_path(spec: SuiteSpec, root: Path | None = None) -> Path:
    return (root or cache_root()) / f"{spec.source}-{spec.dataset_revision}-{spec.dataset_sha256}.data"


def _git_blob_sha256(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def _verify(spec: SuiteSpec, data: bytes) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != spec.dataset_sha256:
        raise OfficialSuiteError(f"{spec.source}: dataset sha256 mismatch, got {actual}")
    if spec.git_blob_sha256 is not None and _git_blob_sha256(data) != spec.git_blob_sha256:
        raise OfficialSuiteError(f"{spec.source}: dataset git blob mismatch")


def fetch_dataset(
    spec: SuiteSpec,
    *,
    cache: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    token: str | None = None,
) -> bytes:
    """Return a verified dataset, atomically populating the user cache on a miss."""
    path = _cache_path(spec, cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        data = path.read_bytes()
        try:
            _verify(spec, data)
        except OfficialSuiteError:
            path.unlink(missing_ok=True)
        else:
            return data
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(transport=transport, follow_redirects=True, timeout=60.0) as client:
            response = client.get(spec.dataset_url, headers=headers)
            response.raise_for_status()
            data = response.content
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise OfficialSuiteError(f"{spec.source}: dataset access denied; configure its access token") from exc
        raise OfficialSuiteError(f"{spec.source}: dataset download failed: {exc}") from exc
    except httpx.HTTPError as exc:
        raise OfficialSuiteError(f"{spec.source}: dataset download failed: {exc}") from exc
    _verify(spec, data)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return data


def _json_rows(data: bytes, spec: SuiteSpec) -> list[dict[str, JsonValue]]:
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise OfficialSuiteError(f"{spec.source}: dataset is not JSON") from exc
    rows = parsed.get("data") if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise OfficialSuiteError(f"{spec.source}: dataset does not contain task rows")
    return [cast(dict[str, JsonValue], row) for row in rows]


def _bu_rows(data: bytes) -> list[dict[str, JsonValue]]:
    try:
        from cryptography.fernet import Fernet, InvalidToken  # the browser extra
    except ImportError as exc:
        raise OfficialSuiteError(f"{BU_BENCH}: reading its dataset needs `parallax-evals[browser]`") from exc
    key = base64.urlsafe_b64encode(hashlib.sha256((BU_SPEC.decrypt_name or "").encode()).digest())
    try:
        decoded = json.loads(Fernet(key).decrypt(base64.b64decode(data)))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise OfficialSuiteError(f"{BU_BENCH}: encrypted dataset could not be read") from exc
    rows = decoded.get("tasks") if isinstance(decoded, dict) else decoded
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise OfficialSuiteError(f"{BU_BENCH}: encrypted dataset does not contain task rows")
    return [cast(dict[str, JsonValue], row) for row in rows]


def _task_id(source: str, upstream_id: str, index: int, used: set[str]) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", upstream_id.lower()).strip("-") or f"task-{index + 1}"
    prefix = f"{source}-"
    digest = hashlib.sha256(upstream_id.encode()).hexdigest()[:10]
    candidate = f"{prefix}{stem}"
    if len(candidate) > 64:
        candidate = f"{candidate[:53].rstrip('-')}-{digest}"
    if candidate in used:
        candidate = f"{candidate[:53].rstrip('-')}-{index + 1:03d}"
    used.add(candidate)
    return candidate


def _url(row: Mapping[str, JsonValue], spec: SuiteSpec) -> str | None:
    value = row.get("website")
    if value is None and spec is BU_SPEC:
        return None
    if not isinstance(value, str) or urlparse(value).scheme not in {"http", "https"} or not urlparse(value).netloc:
        raise OfficialSuiteError(f"{spec.source}: task {row.get('task_id', '<unknown>')} has no usable website")
    return value


def _prompt(row: Mapping[str, JsonValue], spec: SuiteSpec) -> str:
    value = row.get("confirmed_task", row.get("task"))
    if not isinstance(value, str) or not value.strip():
        raise OfficialSuiteError(f"{spec.source}: task {row.get('task_id', '<unknown>')} has no instruction")
    return value


def _record(spec: SuiteSpec, row: dict[str, JsonValue], index: int, used: set[str]) -> TaskRecord:
    upstream_id = str(row.get("task_id", row.get("id", ""))).strip()
    if not upstream_id:
        raise OfficialSuiteError(f"{spec.source}: task {index + 1} has no id")
    instruction = _prompt(row, spec)
    website = _url(row, spec)
    data: JsonObject = {
        "upstream_task_id": upstream_id,
        "confirmed_task": instruction,
    }
    # The agent is told where to begin, as the upstream evaluation asks it; the judge reads the bare instruction.
    prompt = f"Start at {website}. {instruction}" if website is not None else instruction
    if website is not None:
        data["website"] = website
    if spec is MIND2WEB_SPEC:
        data["record"] = row
    return TaskRecord(
        id=_task_id(spec.source, upstream_id, index, used),
        version=1,
        area=Area.BROWSER,
        summary=f"{spec.source} task {upstream_id}",
        prompt=prompt,
        grading=spec.policy,
        requires=frozenset({Capability.BROWSER}),
        maturity="regression",
        provenance=Provenance.CONVERTED,
        data=data,
    )


def _records(spec: SuiteSpec, data: bytes) -> list[TaskRecord]:
    rows = _bu_rows(data) if spec is BU_SPEC else _json_rows(data, spec)
    if len(rows) != spec.expected_count:
        raise OfficialSuiteError(f"{spec.source}: expected {spec.expected_count} tasks, got {len(rows)}")
    used: set[str] = set()
    return [_record(spec, row, index, used) for index, row in enumerate(rows)]


def bu_tasks() -> Iterable[TaskRecord]:
    return _records(BU_SPEC, fetch_dataset(BU_SPEC))


def mind2web_tasks() -> Iterable[TaskRecord]:
    return _records(MIND2WEB_SPEC, fetch_dataset(MIND2WEB_SPEC, token=os.environ.get("HF_TOKEN")))


def _config(req: GradeRequest, spec: SuiteSpec) -> _Config:
    name = "BU_BENCH" if spec is BU_SPEC else "ONLINE_MIND2WEB"
    root_text = os.environ.get(f"PARALLAX_OFFICIAL_{name}_ROOT") or os.environ.get("PARALLAX_OFFICIAL_UPSTREAM_ROOT")
    if not root_text:
        raise OfficialSuiteError("PARALLAX_OFFICIAL_UPSTREAM_ROOT is required for official grading")
    root = Path(root_text).expanduser().resolve()
    python = Path(
        os.environ.get(f"PARALLAX_OFFICIAL_{name}_JUDGE_PYTHON")
        or os.environ.get("PARALLAX_OFFICIAL_JUDGE_PYTHON", root / ".venv" / "bin" / "python")
    )
    config = req.judge_config
    base_url = config.base_url if config is not None else os.environ.get("PARALLAX_JUDGE_BASE_URL", "")
    api_key = config.api_key if config is not None else os.environ.get("PARALLAX_JUDGE_API_KEY", "")
    if not root.is_dir() or not python.is_file() or not base_url or not api_key:
        raise OfficialSuiteError("official judge checkout, interpreter, endpoint and key must be configured")
    return _Config(root=root, python=python.resolve(), base_url=base_url, api_key=api_key)


def _model(req: GradeRequest, spec: SuiteSpec) -> str:
    if spec.judge_model is not None:
        return spec.judge_model
    if req.panel is not None and req.panel.models:
        return req.panel.models[0]
    value = os.environ.get("PARALLAX_OFFICIAL_ONLINE_JUDGE_MODEL")
    if value:
        return value
    raise OfficialSuiteError("Online-Mind2Web requires a configured judge model")


def _identity(spec: SuiteSpec, model: str) -> str:
    return f"{spec.policy}:{spec.source}:judge-{spec.upstream_commit}:model-{model}"


def _policy_identity(spec: SuiteSpec) -> str:
    return _identity(spec, spec.judge_model or os.environ.get("PARALLAX_OFFICIAL_ONLINE_JUDGE_MODEL", "unconfigured"))


def _checks(_task: Task) -> tuple[Check, ...]:
    return (
        RubricCheck(
            id="upstream_judge",
            kind="rubric",
            mode=FailureMode.WRONG_OUTPUT,
            items=("The pinned upstream judge accepts the completed browser task.",),
        ),
    )


def _no_world(_task: Task, _evidence: Evidence, _product: JsonObject | None) -> dict[str, JsonValue]:
    raise MissingEvidence("official browser suites are graded from BrowserEvidence")


def _result(check: Check, passed: bool | None, detail: str) -> CheckResult:
    return CheckResult(
        id=check.id,
        kind=check.kind,
        mode=check.mode,
        gate=check.gate,
        advisory=False,
        weight=check.weight,
        passed=passed,
        detail=detail,
    )


def _unjudged(task: Task, detail: str) -> Graded:
    return Graded(checks=tuple(_result(check, None, detail) for check in _checks(task)))


def _screenshots(evidence: Any) -> list[str]:
    pages = [*evidence.pages, *([evidence.final_page] if evidence.final_page is not None else [])]
    return [page.screenshot_png_b64 for page in pages if page.screenshot_png_b64]


def _steps(evidence: Any) -> list[str]:
    return [json.dumps(step.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) for step in evidence.steps]


def _request(spec: SuiteSpec, req: GradeRequest, evidence: Any, artifact: Path) -> dict[str, JsonValue]:
    task_data = req.task.data
    trace: JsonObject = {
        "final_result": evidence.answer or (req.answers[-1] if req.answers else ""),
        "agent_steps": _steps(evidence),
        "screenshots_b64": _screenshots(evidence),
        "screenshot_steps": None,
        "screenshot_timing": "after",
        "output_files_text": "",
        "evidence_notes": ["BrowserEvidence pages are chronological captures; no step mapping was claimed."],
    }
    model = _model(req, spec)
    payload: dict[str, JsonValue] = {
        "operation": "judge",
        "suite": spec.source,
        "root": str(_config(req, spec).root),
        "commit": spec.upstream_commit,
        "dataset_sha256": spec.dataset_sha256,
        "judge_model": model,
        "reasoning": os.environ.get("PARALLAX_OFFICIAL_REASONING", "xhigh"),
        "artifact": str(artifact),
        "task_id": str(task_data.get("upstream_task_id", "")),
        "task": str(task_data.get("confirmed_task", req.task.turns[0].text)),
        "trace": trace,
        "actions": _steps(evidence),
        "screenshots": _screenshots(evidence),
        "score_threshold": 3,
    }
    if spec is MIND2WEB_SPEC:
        payload["task"] = str(task_data.get("confirmed_task", req.task.turns[0].text))
    return payload


async def _bridge(spec: SuiteSpec, req: GradeRequest, payload: dict[str, JsonValue]) -> dict[str, Any]:
    config = _config(req, spec)
    command = (str(config.python), str(Path(__file__).with_name("official_bridge.py")))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "OPENAI_API_KEY": config.api_key,
        "OPENAI_BASE_URL": config.base_url,
    }
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=config.root,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await process.communicate(json.dumps(payload).encode())
    if process.returncode != 0:
        message = error.decode(errors="replace").strip().splitlines()[-1:] or ["judge bridge failed"]
        raise OfficialSuiteError(message[0])
    try:
        result = json.loads(output)
    except json.JSONDecodeError as exc:
        raise OfficialSuiteError("judge bridge returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise OfficialSuiteError("judge bridge returned a non-object")
    return cast(dict[str, Any], result)


async def _grade(spec: SuiteSpec, req: GradeRequest) -> Graded:
    checks = _checks(req.task)
    interruption = req.execution.get("error")
    if isinstance(interruption, str) and interruption.startswith("environment interruption: "):
        return _unjudged(req.task, interruption)
    try:
        evidence = reported(req.product)
    except MissingEvidence as exc:
        return _unjudged(req.task, f"missing evidence: {exc}")
    if not evidence.observed():
        return _unjudged(req.task, "missing evidence: browser harness reported no observations")
    try:
        _model(req, spec)
        with tempfile.TemporaryDirectory(prefix="parallax-official-") as directory:
            result = await _bridge(spec, req, _request(spec, req, evidence, Path(directory)))
    except (OfficialSuiteError, OSError, ValueError) as exc:
        return _unjudged(req.task, f"missing evidence: official judge unavailable: {exc}")
    if not isinstance(result.get("score"), (int, float)) or not isinstance(result.get("passed"), bool):
        return _unjudged(req.task, "missing evidence: upstream judge returned no valid score")
    score = float(result["score"])
    if not 0 <= score <= 1:
        return _unjudged(req.task, "missing evidence: upstream judge returned an out-of-range score")
    model = _model(req, spec)
    detail = f"upstream judge score={score:.6g}; model={model}; revision={spec.upstream_commit}"
    return Graded(
        checks=tuple(_result(checks[0], bool(result["passed"]), detail) for _ in checks),
        judge_identity=_identity(spec, model),
        score=score,
    )


def _policy(spec: SuiteSpec) -> GradingPolicy:
    judge = JudgePolicy(
        name=f"{spec.policy}:{spec.upstream_commit}",
        questions={"upstream": "The published upstream judge decides the task."},
        evidence_form=f"{spec.source} BrowserEvidence adapter, pinned judge revision {spec.upstream_commit}",
        use_jev=False,
    )
    return GradingPolicy(
        id=spec.policy,
        checks=_checks,
        evidence=_no_world,
        grade=lambda req: _grade(spec, req),
        judge=judge,
        identity=lambda: _policy_identity(spec),
    )


BU_POLICY_EXTENSION = TaskExtension(
    name="official-bu-bench",
    model=OfficialMetadata,
    default_services=False,
    policy=_policy(BU_SPEC),
)
MIND2WEB_POLICY_EXTENSION = TaskExtension(
    name="official-online-mind2web",
    model=OfficialMetadata,
    default_services=False,
    policy=_policy(MIND2WEB_SPEC),
)
