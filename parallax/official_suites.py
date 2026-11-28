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
import signal
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, JsonValue, model_validator

from parallax.browser_evidence import BrowserEvidence, reported
from parallax.extensions import GradeRequest, GradingPolicy, TaskExtension
from parallax.judge import MissingEvidence
from parallax.judging import FALLBACK_MAX_TOKENS, FALLBACK_SAMPLING, Decision, FallbackCall, JudgePolicy, Judgment
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
from parallax.tau2_terms import TerminationReason

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
TAU2 = "tau2"
TAU2_POLICY = "tau2-upstream-v1"
TAU2_UPSTREAM_COMMIT = "768f3d8fe8528b4f32a5096f9ed008ae50f48ead"
TAU2_DOMAINS = ("airline", "retail", "telecom")
# Upstream's agent policy per domain: telecom's is its main policy plus the tech-support manual, the
# procedure the agent walks the customer through (`tau2.domains.telecom.environment.get_environment`).
TAU2_POLICY_FILES = {
    "airline": ("policy.md",),
    "retail": ("policy.md",),
    "telecom": ("main_policy.md", "tech_support_manual.md"),
}
# A domain's version moves when its agent prompt does, so earlier runs never pair with later ones.
TAU2_VERSIONS = {"airline": 1, "retail": 1, "telecom": 2}
TAU2_FILE_SHA256 = {
    ("airline", "tasks.json"): "ccd8ba737b4cc371415af70151187788f728d6108d0916e73bb4317b40542052",
    ("airline", "split_tasks.json"): "b22ced4d9a9850ac9aea31c53bdcb6d6009058140bd9acc7db37c1d36222ba8b",
    ("airline", "policy.md"): "10dc0525421521208be39cee235bba84a16e2bcba9899eb93d92cd81d2f62fc4",
    ("retail", "tasks.json"): "8e03ebce7901bd6218e7a7dc3105faa9324091a68058f7fe61c65262868812e8",
    ("retail", "split_tasks.json"): "ed0580ec52575b63fbf76568af42490da6ee7783ecb4aa81af46961291358f20",
    ("retail", "policy.md"): "2c9652afbce57d6e087768d37cda64d31c53d50b3e3225cfdb791bac66466467",
    ("telecom", "tasks.json"): "37e562e1ae3242577407e1303b1548bc64e7ea68e37d36173e6747990ceaf8a4",
    ("telecom", "split_tasks.json"): "605b488bb9a6acb3c7f4505240a855fdc8681d09aadb16a8f38b2efcfc5c3aec",
    ("telecom", "main_policy.md"): "95943844a0cf11fdf0e2842b483b81d9f9338aa53235712ed131db075d086ed7",
    ("telecom", "tech_support_manual.md"): "015a3ee49ec8c199c5e1a059c922081937b92cf3a5ea74fedbc4357816f389ba",
}
TAU2_EXPECTED_BASE = {"airline": 50, "retail": 114, "telecom": 114}
# Upstream benchmark defaults at the pin (`tau2.config`), named by their OpenRouter ids, since
# OpenRouter is Parallax's judge gateway: each id's OpenRouter canonical slug is the upstream
# snapshot (`openai/gpt-5.5-20260423`, `openai/gpt-5.4-mini-20260317`, `openai/gpt-4.1-2025-04-14`).
# The semantic communicate judge and the NL-assertion judge run through the configured judge
# gateway under these ids, validated by the preflight in `_grade_tau2` before any bridge process starts.
TAU2_COMMUNICATE_JUDGE_MODEL = "openai/gpt-5.5"
TAU2_NL_JUDGE_MODEL = "openai/gpt-5.4-mini"
# The user simulator's model: upstream's default at the pin (`tau2.config.DEFAULT_LLM_USER`),
# run by parallax through the configured gateway; its cost is simulator cost, never harness usage.
TAU2_SIMULATOR_MODEL = "openai/gpt-4.1"
TAU2_BRIDGE_TIMEOUT_S = 900.0


class Tau2ToolCall(Model):
    id: str = ""
    name: str
    # Upstream `tau2.data_model.message.ToolCall.arguments` is a dict; a string form is rejected.
    arguments: dict[str, JsonValue] = {}
    requestor: Literal["assistant", "user"] = "assistant"


class Tau2Message(Model):
    model_config = {"extra": "allow", "frozen": True}

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: tuple[Tau2ToolCall, ...] | None = None
    id: str | None = None
    requestor: Literal["assistant", "user"] = "assistant"
    error: bool = False


class Tau2Evidence(Model):
    """Parallax's own recorded tau2 trial evidence: the full upstream transcript and how it ended.

    The runner records it from its tool server's log and the turns it drove; harness-reported
    evidence is never graded. Every assistant tool call must be followed, in order, by its
    `ToolMessage`(s); the evaluator replays those calls itself, so no separate tool-call list
    or final database state is accepted. `termination_reason` is required, with no default.
    """

    transcript: tuple[Tau2Message, ...] = Field(min_length=1)
    termination_reason: TerminationReason

    @model_validator(mode="after")
    def _tool_calls_are_followed_by_tool_messages(self) -> Tau2Evidence:
        messages = self.transcript
        for index, message in enumerate(messages):
            if message.role != "assistant" or not message.tool_calls:
                continue
            following = messages[index + 1 : index + 1 + len(message.tool_calls)]
            if len(following) != len(message.tool_calls) or any(m.role != "tool" for m in following):
                raise ValueError(
                    f"transcript[{index}]: assistant tool calls must each be followed by their ToolMessage, in order"
                )
            for call, tool_message in zip(message.tool_calls, following, strict=True):
                if call.id and tool_message.id not in (None, call.id):
                    raise ValueError(
                        f"transcript[{index}]: ToolMessage id {tool_message.id!r} does not match tool call {call.id!r}"
                    )
        return self


def parse_tau2_evidence(recorded: JsonObject | None) -> Tau2Evidence:
    """Validate the transcript parallax itself recorded (`GradeRequest.runner_recorded`).

    Harness-reported evidence is never accepted here: only the runner's own record grades.
    Raises ValueError with the reason when it cannot grade.
    """
    if not isinstance(recorded, dict):
        raise ValueError("parallax recorded no tau2 transcript for this trial")
    return Tau2Evidence.model_validate(recorded)


def tau2_task_digest(task: JsonObject) -> str:
    """SHA-256 over the canonical JSON of a full pinned tau2 task, as stored in `TaskRecord.data`."""
    return hashlib.sha256(json.dumps(task, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tau2_task_from_data(data: JsonObject) -> JsonObject:
    """The full pinned task JSON from a tau2 record's data, digest-checked; raises on mismatch."""
    task = data.get("task")
    if not isinstance(task, dict):
        raise OfficialSuiteError("tau2 task data carries no full pinned task JSON")
    if data.get("task_digest") != tau2_task_digest(task):
        raise OfficialSuiteError("tau2 task JSON failed its digest check")
    return task


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


def tau2_policy(domain: str) -> str:
    """The agent policy upstream's environment builds for a domain, byte for byte."""
    texts = [_fetch_tau2_file(domain, name).decode() for name in TAU2_POLICY_FILES[domain]]
    match texts:
        case [policy]:
            return policy
        case [main, tech_support]:
            return (
                f"<main_policy>\n{main}\n</main_policy>\n<tech_support_policy>\n{tech_support}\n</tech_support_policy>"
            )
        case _:
            raise OfficialSuiteError(f"{TAU2}: {domain} names {len(texts)} policy files")


def _fetch_tau2_file(domain: str, name: str) -> bytes:
    spec = SuiteSpec(
        source=f"{TAU2}-{domain}",
        policy=TAU2_POLICY,
        upstream_commit=TAU2_UPSTREAM_COMMIT,
        dataset_url=(
            f"https://raw.githubusercontent.com/sierra-research/tau2-bench/{TAU2_UPSTREAM_COMMIT}"
            f"/data/tau2/domains/{domain}/{name}"
        ),
        dataset_sha256=TAU2_FILE_SHA256[(domain, name)],
        dataset_revision=TAU2_UPSTREAM_COMMIT,
        expected_count=1,
    )
    return fetch_dataset(spec)


def _tau2_json(domain: str, name: str) -> Any:
    try:
        return json.loads(_fetch_tau2_file(domain, name))
    except json.JSONDecodeError as exc:
        raise OfficialSuiteError(f"{TAU2}: {domain}/{name} is not JSON") from exc


def tau2_tasks() -> Iterable[TaskRecord]:
    """One world-less record per (domain, base-split task id); parallax drives the simulator loop.

    Each record carries the full pinned task JSON (`data["task"]`, digest-checked in
    `data["task_digest"]`) so the runner can build the simulator from the task's own scenario.
    The scenario never enters the prompt, which carries only the domain policy, as upstream.
    """
    records: list[TaskRecord] = []
    used: set[str] = set()
    for domain in TAU2_DOMAINS:
        rows = _tau2_json(domain, "tasks.json")
        splits = _tau2_json(domain, "split_tasks.json")
        if not isinstance(rows, list) or not isinstance(splits, dict) or not isinstance(splits.get("base"), list):
            raise OfficialSuiteError(f"{TAU2}: {domain} dataset has no base split")
        by_id = {str(row.get("id")): row for row in rows if isinstance(row, dict) and row.get("id") is not None}
        base = [str(task_id) for task_id in splits["base"]]
        if len(base) != TAU2_EXPECTED_BASE[domain] or any(task_id not in by_id for task_id in base):
            raise OfficialSuiteError(f"{TAU2}: {domain} base split does not match its task list")
        policy = tau2_policy(domain)
        files = "+".join(TAU2_POLICY_FILES[domain])
        policy_ref = f"data/tau2/domains/{domain}/{files}@{TAU2_UPSTREAM_COMMIT}"
        for upstream_id in base:
            prompt = (
                f"{policy.strip()}\n\n---\n\nYou are the {domain} customer-service agent. A simulated "
                "customer will describe their request over several turns; use the domain tools and follow "
                "the policy above."
            )
            full_task = cast(JsonObject, by_id[upstream_id])
            records.append(
                TaskRecord(
                    id=_task_id(TAU2, f"{domain}-{upstream_id}", len(records), used),
                    version=TAU2_VERSIONS[domain],
                    area=Area.SAAS,
                    summary=f"{TAU2} {domain} task {upstream_id}",
                    prompt=prompt,
                    grading=TAU2_POLICY,
                    requires=frozenset({Capability.MCP, Capability.MULTI_TURN}),
                    maturity="regression",
                    provenance=Provenance.CONVERTED,
                    data={
                        "domain": domain,
                        "upstream_task_id": upstream_id,
                        "policy_ref": policy_ref,
                        "task": full_task,
                        "task_digest": tau2_task_digest(full_task),
                    },
                )
            )
    return records


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
    # Absolute but not resolved: a virtual environment's `python` is a symlink to its base interpreter, and only
    # launched through the link does it find the environment's `pyvenv.cfg` and the judge's installed packages.
    return _Config(root=root, python=python.expanduser().absolute(), base_url=base_url, api_key=api_key)


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
    identity = f"{spec.policy}:{spec.source}:judge-{spec.upstream_commit}:model-{model}"
    if spec is MIND2WEB_SPEC:
        # Graded under upstream's 512-token reply cap, a reasoning judge failed trials it passes with the full budget:
        # the two are different judges, and their verdicts must not pool into one score.
        sampling = json.dumps(FALLBACK_SAMPLING.get(model, {}), sort_keys=True, separators=(",", ":"))
        identity += f":max-tokens-{FALLBACK_MAX_TOKENS}:sampling-{sampling}"
    return identity


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


def _screenshots(evidence: Any) -> list[JsonValue]:
    pages = [*evidence.pages, *([evidence.final_page] if evidence.final_page is not None else [])]
    return [page.screenshot_png_b64 for page in pages if page.screenshot_png_b64]


def _steps(evidence: Any) -> list[JsonValue]:
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
        payload["max_tokens"] = FALLBACK_MAX_TOKENS
        payload["sampling"] = FALLBACK_SAMPLING.get(model, {})
    return payload


async def _bridge(spec: SuiteSpec, req: GradeRequest, payload: dict[str, JsonValue]) -> dict[str, Any]:
    config = _config(req, spec)
    # `-B`: the pinned checkout tracks its own `__pycache__`, so a judge that wrote bytecode would leave tracked
    # changes, and the bridge refuses every later call on a checkout that differs from its pin.
    command = (str(config.python), "-B", str(Path(__file__).with_name("official_bridge.py")))
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
        model = _model(req, spec)
        _config(req, spec)
    except (OfficialSuiteError, OSError, ValueError) as exc:
        return _unjudged(req.task, f"missing evidence: official judge unavailable: {exc}")
    return await _upstream(spec, req, evidence, model)


UPSTREAM_QUESTIONS = {"upstream": "The published upstream judge decides the task."}


def _judgment(
    spec: SuiteSpec, model: str, payload: Mapping[str, JsonValue], result: Mapping[str, Any]
) -> Judgment | None:
    """The bridge's measured calls as a judgment the runner prices, with the upstream judge's verdict and its
    reasoning so an audit can read why a trial passed or failed; None when it reported no calls."""
    calls = result.get("calls")
    if not isinstance(calls, list) or not calls:
        return None
    passed, reason = result.get("passed"), result.get("reason")
    decisions = (
        {"upstream": Decision(passed=passed, source="llm", reason=reason)}
        if isinstance(passed, bool) and isinstance(reason, str)
        else {}
    )
    return Judgment(
        policy=spec.policy,
        identity=_identity(spec, model),
        state_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        questions=UPSTREAM_QUESTIONS,
        decisions=decisions,
        fallback_calls=tuple(
            FallbackCall.model_validate({**call, "model": model, "attempt": index}) for index, call in enumerate(calls)
        ),
    )


async def _upstream(spec: SuiteSpec, req: GradeRequest, evidence: BrowserEvidence, model: str) -> Graded:
    checks = _checks(req.task)
    try:
        with tempfile.TemporaryDirectory(prefix="parallax-official-") as directory:
            payload = _request(spec, req, evidence, Path(directory))
            result = await _bridge(spec, req, payload)
    except (OfficialSuiteError, OSError, ValueError) as exc:
        # The bridge may have reached the judge before failing, and a failed bridge reports no calls, so the
        # judge cost is unknown.
        unjudged = _unjudged(req.task, f"missing evidence: official judge unavailable: {exc}")
        return unjudged.model_copy(update={"unpriced_calls": 1})
    judgment = _judgment(spec, model, payload, result)
    # A bridge that reported no calls leaves the judge cost unknown.
    judgments, unpriced = ((judgment,), 0) if judgment is not None else ((), 1)
    spend = {"judgments": judgments, "unpriced_calls": unpriced}
    if not isinstance(result.get("score"), (int, float)) or not isinstance(result.get("passed"), bool):
        return _unjudged(req.task, "missing evidence: upstream judge returned no valid score").model_copy(update=spend)
    score = float(result["score"])
    if not 0 <= score <= 1:
        unjudged = _unjudged(req.task, "missing evidence: upstream judge returned an out-of-range score")
        return unjudged.model_copy(update=spend)
    detail = f"upstream judge score={score:.6g}; model={model}; revision={spec.upstream_commit}"
    return Graded(
        checks=tuple(_result(checks[0], bool(result["passed"]), detail) for _ in checks),
        judge_identity=_identity(spec, model),
        score=score,
        judgments=judgments,
        unpriced_calls=unpriced,
    )


def _policy(spec: SuiteSpec) -> GradingPolicy:
    judge = JudgePolicy(
        name=f"{spec.policy}:{spec.upstream_commit}",
        questions=UPSTREAM_QUESTIONS,
        evidence_form=f"{spec.source} BrowserEvidence adapter, pinned judge revision {spec.upstream_commit}",
    )
    return GradingPolicy(
        id=spec.policy,
        checks=_checks,
        evidence=_no_world,
        grade=lambda req: _grade(spec, req),
        judge=judge,
        identity=lambda: _policy_identity(spec),
    )


def _tau2_checks(_task: Task) -> tuple[Check, ...]:
    return (
        RubricCheck(
            id="upstream_evaluator",
            kind="rubric",
            mode=FailureMode.WRONG_OUTPUT,
            items=("The pinned upstream tau2 evaluate_simulation accepts the completed simulation.",),
        ),
    )


def _tau2_unjudged(task: Task, detail: str) -> Graded:
    return Graded(checks=tuple(_result(check, None, detail) for check in _tau2_checks(task)))


def _tau2_root() -> tuple[Path, Path]:
    root_text = os.environ.get("PARALLAX_OFFICIAL_TAU2_ROOT") or os.environ.get("PARALLAX_OFFICIAL_UPSTREAM_ROOT")
    if not root_text:
        raise OfficialSuiteError("PARALLAX_OFFICIAL_TAU2_ROOT is required for tau2 grading")
    root = Path(root_text).expanduser().resolve()
    python = Path(
        os.environ.get("PARALLAX_OFFICIAL_TAU2_PYTHON")
        or os.environ.get("PARALLAX_OFFICIAL_UPSTREAM_PYTHON", root / ".venv" / "bin" / "python")
    )
    if not root.is_dir() or not python.is_file():
        raise OfficialSuiteError("tau2 checkout and its interpreter must be configured")
    return root, python.expanduser().absolute()


def _tau2_judge_env(req: GradeRequest) -> dict[str, str]:
    """Only the judge's endpoint and key cross into the bridge, through an explicit allowlist.

    Tau2's semantic communicate and NL-assertion judges call LiteLLM, which reads the
    OpenAI-compatible endpoint from these names. Simulator keys never enter the bridge.
    """
    config = req.judge_config
    base_url = config.base_url if config is not None else os.environ.get("PARALLAX_JUDGE_BASE_URL", "")
    api_key = config.api_key if config is not None else os.environ.get("PARALLAX_JUDGE_API_KEY", "")
    env: dict[str, str] = {}
    if base_url:
        env["OPENAI_BASE_URL"] = base_url
    if api_key:
        env["OPENAI_API_KEY"] = api_key
    return env


def _read_judge_calls(calls_file: Path) -> list[dict[str, Any]]:
    """Judge calls recorded as they happened, from the side file: the one judge-call channel."""
    calls: list[dict[str, Any]] = []
    if calls_file.is_file():
        for line in calls_file.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                calls.append(entry)
    return calls


def _bridge_error(error_text: str) -> str:
    """Grade detail for a failed bridge: the stderr exception class and a fixed reason only.

    Raw bridge stderr never reaches a grade: it can carry judge responses and request detail.
    """
    last = error_text.strip().splitlines()[-1:][0].strip() if error_text.strip() else ""
    label = last.split(":", 1)[0].strip()
    if not re.fullmatch(r"[A-Za-z_][\w.]*", label or ""):
        label = "Error"
    return f"{label}: tau2 bridge failed"


async def _tau2_bridge(req: GradeRequest, payload: dict[str, JsonValue]) -> dict[str, Any]:
    """Run the bridge bounded by `TAU2_BRIDGE_TIMEOUT_S`; its process group is killed on timeout
    or cancellation. Judge calls recorded before a failure come back in `calls` either way, and a
    failed bridge returns `{"error": ..., "calls": [...]}` instead of raising."""
    root, python = _tau2_root()
    command = (str(python), "-B", str(Path(__file__).with_name("tau2_bridge.py")))
    env = {"PATH": os.environ.get("PATH", ""), "LANG": os.environ.get("LANG", "C.UTF-8"),
           "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"), **_tau2_judge_env(req)}  # fmt: skip
    with tempfile.TemporaryDirectory(prefix="parallax-tau2-") as directory:
        calls_file = Path(directory) / "judge-calls.jsonl"
        payload = {**payload, "root": str(root), "calls_file": str(calls_file)}
        process = await asyncio.create_subprocess_exec(
            *command, cwd=root, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )  # fmt: skip
        timed_out = False
        output, error = b"", b""
        try:
            output, error = await asyncio.wait_for(
                process.communicate(json.dumps(payload).encode()), timeout=TAU2_BRIDGE_TIMEOUT_S
            )
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            await process.wait()
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            await process.wait()
            raise
        error_text = error.decode(errors="replace")
        if timed_out:
            return {
                "error": f"tau2 bridge timed out after {TAU2_BRIDGE_TIMEOUT_S:.0f}s",
                "calls": _read_judge_calls(calls_file),
            }
        calls = _read_judge_calls(calls_file)
        if process.returncode != 0:
            return {"error": _bridge_error(error_text), "calls": calls}
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            return {"error": "tau2 bridge returned invalid JSON", "calls": calls}
        if not isinstance(result, dict):
            return {"error": "tau2 bridge returned a non-object", "calls": calls}
        return {**cast(dict[str, Any], result), "calls": calls}


def _tau2_judgment(req: GradeRequest, payload: Mapping[str, JsonValue], result: Mapping[str, Any]) -> Judgment | None:
    """The bridge's judge calls as a priced judgment; None when the evaluator made none."""
    calls = result.get("calls")
    if not isinstance(calls, list) or not calls:
        return None
    passed, reason = result.get("passed"), result.get("reason")
    decisions = (
        {"upstream": Decision(passed=passed, source="llm", reason=reason)}
        if isinstance(passed, bool) and isinstance(reason, str)
        else {}
    )
    return Judgment(
        policy=TAU2_POLICY,
        identity=f"{TAU2_POLICY}:{TAU2}:evaluator-{TAU2_UPSTREAM_COMMIT}",
        state_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        questions={"upstream": "The pinned upstream tau2 evaluator decides the task."},
        decisions=decisions,
        fallback_calls=tuple(
            FallbackCall.model_validate({**call, "attempt": index})
            for index, call in enumerate(calls)
            if isinstance(call, dict)
        ),
    )


# Successful gateway probes are cached per process: keyed by gateway, the key's SHA-256
# fingerprint prefix (never the key itself) and both judge ids. Failures are not cached, so a
# later process (or a retry after a transient gateway failure) probes again.
_TAU2_PREFLIGHT_CACHE: dict[tuple[str, str, str, str], str | None] = {}


async def _tau2_gateway_models(base_url: str, api_key: str) -> set[str]:
    """The model ids the configured judge gateway offers, from its OpenAI-compatible list."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {api_key}"})
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise OfficialSuiteError("tau2 judge gateway returned no model list")
    return {str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")}


def _key_fingerprint(api_key: str) -> str:
    """The cache-key fingerprint of a gateway key: a SHA-256 prefix, never the key itself."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


async def _tau2_preflight(req: GradeRequest) -> str | None:
    """Query the judge gateway's model list and refuse when either judge id is missing.

    A successful probe is cached per process, keyed by gateway, key fingerprint (see
    `_key_fingerprint`) and both judge ids; failures are not cached. A missing judge endpoint
    or key, an unreachable gateway and a missing checkout each refuse before any bridge process
    or judge spend starts.
    """
    env = _tau2_judge_env(req)
    if not env.get("OPENAI_BASE_URL") or not env.get("OPENAI_API_KEY"):
        return "tau2 judge gateway is not configured (judge endpoint and key are required)"
    try:
        _tau2_root()
    except OfficialSuiteError as exc:
        return str(exc)
    key = (
        env["OPENAI_BASE_URL"],
        _key_fingerprint(env["OPENAI_API_KEY"]),
        TAU2_COMMUNICATE_JUDGE_MODEL,
        TAU2_NL_JUDGE_MODEL,
    )
    if key in _TAU2_PREFLIGHT_CACHE:
        return _TAU2_PREFLIGHT_CACHE[key]
    result: str | None
    try:
        offered = await _tau2_gateway_models(env["OPENAI_BASE_URL"], env["OPENAI_API_KEY"])
    except (httpx.HTTPError, OSError, ValueError, OfficialSuiteError) as exc:
        result = f"tau2 judge gateway model list could not be read ({type(exc).__name__})"
    else:
        missing = [model for model in (TAU2_COMMUNICATE_JUDGE_MODEL, TAU2_NL_JUDGE_MODEL) if model not in offered]
        result = f"tau2 judge model(s) not offered by the configured gateway: {', '.join(missing)}" if missing else None
    if result is None:  # failures are not cached: a later grade probes the gateway again
        _TAU2_PREFLIGHT_CACHE[key] = result
    return result


async def _grade_tau2(req: GradeRequest) -> Graded:
    interruption = req.execution.get("error")
    if isinstance(interruption, str) and interruption.startswith("environment interruption: "):
        return _tau2_unjudged(req.task, interruption)
    try:
        evidence = parse_tau2_evidence(req.runner_recorded)
    except ValueError as exc:
        return _tau2_unjudged(req.task, f"missing evidence: invalid tau2 evidence: {exc}")
    if (preflight := await _tau2_preflight(req)) is not None:
        return _tau2_unjudged(req.task, f"missing evidence: tau2 judge preflight failed: {preflight}")
    data = req.task.data
    try:
        tau2_task_from_data(data)  # the recorded trial must belong to the digest-checked pinned task
    except OfficialSuiteError as exc:
        return _tau2_unjudged(req.task, f"missing evidence: {exc}")
    # Simulator spend is parallax's own record: it is not forwarded and never priced here.
    payload: dict[str, JsonValue] = {
        "operation": "evaluate",
        "suite": TAU2,
        "commit": TAU2_UPSTREAM_COMMIT,
        "domain": str(data.get("domain", "")),
        "task_id": str(data.get("upstream_task_id", "")),
        "transcript": cast(JsonValue, evidence.model_dump(mode="json")["transcript"]),
        "termination_reason": evidence.termination_reason,
        "judge_models": {"communicate": TAU2_COMMUNICATE_JUDGE_MODEL, "nl": TAU2_NL_JUDGE_MODEL},
    }
    try:
        result = await _tau2_bridge(req, payload)
    except (OfficialSuiteError, OSError, ValueError) as exc:
        # No judge calls were recorded, so zero calls are recorded: no unpriced call stands in.
        return _tau2_unjudged(req.task, f"missing evidence: upstream tau2 evaluator unavailable: {type(exc).__name__}")
    if isinstance(result.get("error"), str):
        # Judge calls recorded before the failure still price the grade; with none recorded,
        # zero calls are recorded, not one unpriced call.
        judgment = _tau2_judgment(req, payload, result)
        spend = {"judgments": (judgment,) if judgment is not None else (), "unpriced_calls": 0}
        return _tau2_unjudged(
            req.task, f"missing evidence: upstream tau2 evaluator unavailable: {result['error']}"
        ).model_copy(update=spend)
    judgment = _tau2_judgment(req, payload, result)
    judgments = (judgment,) if judgment is not None else ()
    spend = {"judgments": judgments, "unpriced_calls": 0}
    if not isinstance(result.get("score"), (int, float)) or not isinstance(result.get("passed"), bool):
        return _tau2_unjudged(req.task, "missing evidence: upstream tau2 evaluator returned no valid score").model_copy(
            update=spend
        )
    score = float(result["score"])
    if not 0 <= score <= 1:
        return _tau2_unjudged(
            req.task, "missing evidence: upstream tau2 evaluator returned an out-of-range score"
        ).model_copy(update=spend)
    detail = f"upstream tau2 reward={score:.6g}; revision={TAU2_UPSTREAM_COMMIT}"
    return Graded(
        checks=tuple(_result(check, bool(result["passed"]), detail) for check in _tau2_checks(req.task)),
        judge_identity=f"{TAU2_POLICY}:{TAU2}:evaluator-{TAU2_UPSTREAM_COMMIT}",
        score=score,
        judgments=judgments,
        unpriced_calls=0,
    )


TAU2_POLICY_OBJECT = GradingPolicy(
    id=TAU2_POLICY,
    checks=_tau2_checks,
    evidence=_no_world,
    grade=_grade_tau2,
    judge=JudgePolicy(
        name=f"{TAU2_POLICY}:{TAU2_UPSTREAM_COMMIT}",
        questions={"upstream": "The pinned upstream tau2 evaluator decides the task."},
        evidence_form=f"tau2 full transcript and termination_reason, pinned revision {TAU2_UPSTREAM_COMMIT}",
    ),
    identity=lambda: f"{TAU2_POLICY}:{TAU2}:evaluator-{TAU2_UPSTREAM_COMMIT}",
)

TAU2_POLICY_EXTENSION = TaskExtension(
    name="official-tau2",
    model=OfficialMetadata,
    default_services=False,
    policy=TAU2_POLICY_OBJECT,
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
