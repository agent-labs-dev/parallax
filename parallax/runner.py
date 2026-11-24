"""Trial planning, the conversation schedule, evidence assembly and grading.

The runner owns the schedule: a harness sees one turn at a time, and between turns the runner
moves the world (position, patches, clock, scheduler fires). Evidence comes only from parallax-owned
sources (world, workspace snapshot, trusted verify, captured answers); harness traces are stored
for diagnosis and never graded.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Coroutine, Generator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, assert_never, cast, runtime_checkable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from pydantic import JsonValue
from starlette.types import ASGIApp

from parallax import __version__, extensions, interrupts, provenance
from parallax.assets import TrialAssets
from parallax.box import (
    Box,
    BoxError,
    BoxKind,
    DockerBox,
    Sidecar,
    box_kind,
    desktop_box,
    open_box,
    seed_dir,
    snapshot,
    trusted_verify,
    web_isolation_for,
)
from parallax.harnesses.base import (
    RAW_OUTPUT_CHARS,
    HarnessError,
    OutputTail,
    config_hash,
    credentials_unusable,
    load_harness,
    native_credentials,
)
from parallax.harnesses.manifest import wire_model_name
from parallax.harnesses.parsers import turn_answer
from parallax.judge import JudgeConfig, JudgePanel, config_from_env, panel_from_env
from parallax.meter import (
    DEFAULT_UPSTREAM,
    BudgetRefused,
    CampaignBudget,
    MeterClient,
    MeterReport,
    llm_endpoint,
    model_price_known,
    new_trial_key,
    trial_wire,
    upstreams_from_env,
)
from parallax.meter import app as meter_app
from parallax.models import (
    ENVIRONMENT_INTERRUPTION,
    ApprovalAnswer,
    ApprovalDecision,
    Area,
    Capability,
    CheckResult,
    CommandCheck,
    CommandOutcome,
    CostSource,
    Evidence,
    FileCheck,
    Fire,
    HarnessInfo,
    HarnessKind,
    Isolation,
    JsonObject,
    LatencySplit,
    McpEndpoint,
    Model,
    RunManifest,
    RunOrigin,
    Step,
    StepKind,
    Task,
    Timing,
    Trajectory,
    TrajectoryStatus,
    Trial,
    TrialFlag,
    TrialResult,
    Turn,
    TurnInput,
    TurnOutput,
    TurnStatus,
    Usage,
    Verdict,
    WebIsolation,
    WorldEvidence,
    WorldPatch,
    fingerprint,
)
from parallax.protocols import Harness, TrialCosts, TrialDetails, TrialTiming
from parallax.semantic import grade_checks, policy_identity, record_grade
from parallax.task import (
    DATE_PLACEHOLDER,
    LITE,
    fill_dates,
    load_patch,
    members,
    mounted_services,
    rebind_persona,
    rebind_value,
)

if TYPE_CHECKING:
    from parallax.tau2_runtime import Tau2BridgeRuntime, Tau2ToolLog

from parallax.track.live import LiveFeed
from parallax.track.live import label as live_label
from parallax.track.rows import is_validity_control
from parallax.world.client import ControlClient, ControlError, admin_register
from parallax.world.core import World, WorldError
from parallax.world.server import WorldServer

logger = logging.getLogger(__name__)

TOKEN_ENV = "PARALLAX_WORLD_TOKEN"
RESULTS = "results.jsonl"
BUDGET_JOURNAL = "budget.jsonl"
INVALID_EXIT_SHARE = 0.10
# Every reason a trial's own execution can go INVALID for and another attempt can plausibly avoid. A
# reason outside this list is not retried, because re-running cannot measure anything new: a harness or
# box the run could not open would fail its probe again, missing evidence describes the trial's own
# world state, a validity rejection is a judgment of the evidence that attempt produced, and a failed
# teardown would strand more of the harness's remote resources rather than fewer.
RETRYABLE_INVALID = (
    "boot_error",
    "start: ",  # the harness's start raised; `finish` records it as the boot_error detail
    "box: ",  # the trial's box could not be opened
    "execution incomplete: ",
    "world registration failed: ",
    "world control failed mid-trial: ",
    "box clock could not follow the world to ",
    "world evidence unavailable: ",
    "world internal error: ",
    "meter unreachable: ",
    ENVIRONMENT_INTERRUPTION,  # a site or provider outage the harness reported; waited out (`retry_wait_s`)
)
NEVER_RETRYABLE_INVALID = (
    " box unavailable: ",
    "harness unavailable: ",
    "harness teardown failed: ",
    "semantic judge rejected execution validity",
    "teardown failed for ",
)
FIRE_TEXT = 'Scheduled task "{title}" is due now ({at}). Instructions you saved: {instruction}'
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET", re.IGNORECASE)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{8,}"),
)
_MIN_SECRET_LEN = 8  # shorter env values ("1", "yes") would redact ordinary words
_UNSAFE_PATH = re.compile(r"[^A-Za-z0-9._-]+")
_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
REDACTED = "[REDACTED]"


class RunOptions(Model):
    out: Path
    repeats: int = 1
    isolation: Isolation = Isolation.DOCKER
    concurrency: int = 4
    world_public_url: str | None = None
    world_port: int | None = None  # remote isolation: the fixed port a tunnel points at (None: any free port)
    judge: tuple[str, ...] = ()
    include_xfail: bool = False
    # Further attempts for a trial that ended INVALID for a transient execution fault (RETRYABLE_INVALID).
    retry_invalid: int = 1
    turn_timeout_s: float | None = None  # None: a turn ends when the product ends it
    selection: str = ""
    quiet: bool = False
    # Recorded in provenance.json: values only the caller knows.
    provenance_extra: dict[str, str] = {}
    # (task id, model) pairs: run only these trials of the task x model product. Empty: the whole product.
    pairs: tuple[tuple[str, str], ...] = ()
    # Seconds before the first retry round; each later round waits twice the one before. 0: retry at once.
    retry_wait_s: float = 0.0
    # The campaign's spend limit in USD across this run directory and every resume of it (`meter.CampaignBudget`);
    # None runs unbudgeted. Each trial reserves `reserve_usd` before it starts, settled from its cost after.
    budget_usd: float | None = None
    reserve_usd: float | None = None
    # Where the budget's journal lives; None: `budget.jsonl` in this run directory. Runs run one after another on one
    # journal share one limit, so several run directories (arms that need different models) spend one budget.
    budget_journal: Path | None = None
    # Where desktop trials' videos are kept, one per trial directory name; None keeps each in its trial directory,
    # which an upload deletes. A separate directory outlives the upload, for a workflow artifact.
    videos: Path | None = None


@runtime_checkable
class TaskBound(Protocol):
    """A harness that needs task declarations before setup (oracle replay or real accounts)."""

    def bind_task(self, task: Task) -> None: ...


@runtime_checkable
class AccountBound(Protocol):
    """A harness whose agent acts as an account the task cannot choose, such as a product login minted
    per trial. `account` runs after `bind_task` and before the world is seeded, and returns that account's
    email (None: the persona's own). The task is then rebound to it (`rebind_persona`) and bound again, so
    the world and its grading know the user by the one address the agent sees."""

    async def account(self, trial_id: str) -> str | None: ...


@runtime_checkable
class Configured(Protocol):
    @property
    def config_hash(self) -> str: ...


@runtime_checkable
class RawLogged(Protocol):
    """A harness that keeps its raw process output (bounded) for the trial's harness.log."""

    output_tail: OutputTail


@runtime_checkable
class StderrTailed(Protocol):
    stderr_tail: str


@runtime_checkable
class Caveated(Protocol):
    """A harness that learns, as late as its own cleanup, what the trial's row must carry: reasons the
    verdict is INVALID, and notes beside it. Read after `stop`."""

    trial_invalid: list[str]
    trial_notes: list[str]


@runtime_checkable
class TrialMetadata(Protocol):
    """Secret-free structured setup facts that semantic evidence may need to interpret."""

    trial_metadata: JsonObject


@runtime_checkable
class TrialMetrics(Protocol):
    """Named measurements a harness reports for the trial row (`TrialResult.metrics`). Read after `stop`."""

    trial_metrics: dict[str, float]


@runtime_checkable
class TurnTimed(Protocol):
    """A harness that can say how much of a turn that raised was the agent's own time: its wait on
    the agent, not the bookkeeping it does after (reading usage back, say). None when it cannot say."""

    def agent_elapsed_ms(self) -> int | None: ...


async def finish_uncancelled(cleanup: Awaitable[None]) -> None:
    """Run `cleanup` to its end even when the caller is cancelled (SIGINT, a cancelled run): an
    interrupted `stop` strands the harness's remote resources. The cancel is re-raised afterwards."""
    task = asyncio.ensure_future(cleanup)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Ours or the cleanup's own: either way the caller's cancel must still land.
            interrupted = True
    if interrupted:
        if not task.cancelled():
            task.exception()  # retrieved, so it is not reported as never retrieved; the cancel wins
        raise asyncio.CancelledError
    task.result()


def run_to_teardown[T](main: Coroutine[None, None, T]) -> T:
    """`asyncio.run`, but every SIGINT or SIGTERM only cancels `main`. A second one (a repeated Ctrl-C, `uv run`
    forwarding its copy of the terminal's, or a CI runner's SIGTERM after its SIGINT) would otherwise raise
    KeyboardInterrupt or end the process mid-teardown and strand boxes and paid browsers. The first also starts the
    `interrupts` hooks, ahead of the unwinding, and the run ends only once they have finished."""

    async def guarded() -> T:
        hooks: asyncio.Task[None] | None = None
        task = asyncio.current_task()

        def interrupt() -> None:
            nonlocal hooks
            if hooks is None:  # created before the cancel, so the hooks see the run as it was
                hooks = asyncio.ensure_future(interrupts.interrupted())
            if task is not None:
                task.cancel()

        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, interrupt)
        try:
            return await main
        finally:
            while hooks is not None and not hooks.done():
                with contextlib.suppress(asyncio.CancelledError):  # a repeated signal must not cut the hooks short
                    await asyncio.shield(hooks)

    try:
        return asyncio.run(guarded())
    except asyncio.CancelledError:
        raise KeyboardInterrupt from None


# ---------------------------------------------------------------------------
# redaction, paths, placement
# ---------------------------------------------------------------------------


def secret_values(extra: Sequence[str] = ()) -> tuple[str, ...]:
    host = (v for k, v in os.environ.items() if _SECRET_NAME.search(k) and len(v) >= _MIN_SECRET_LEN)
    return tuple(sorted({*host, *(s for s in extra if len(s) >= _MIN_SECRET_LEN)}, key=len, reverse=True))


def redact(text: str, secrets_: Sequence[str] = ()) -> str:
    for value in secrets_:
        text = text.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def missing_task_secret_env(task: Task) -> tuple[tuple[str, str], ...]:
    """Return task secret references whose host variables are absent or empty, without reading their values out."""
    return tuple((alias, env_name) for alias, env_name in task.secrets.items() if not os.environ.get(env_name))


def safe_segment(value: str | None) -> str:
    return "-" if value is None else _UNSAFE_PATH.sub("_", value)


def trial_rel_dir(task_id: str, harness: str, model: str | None, repeat: int, attempt: int = 0) -> str:
    """A trial's directory. A retry (`attempt` > 0) gets its own beside the first, so no attempt overwrites another."""
    suffix = f"-attempt{attempt}" if attempt else ""
    return f"trials/{task_id}/{harness}/{safe_segment(model)}/{repeat}{suffix}"


def normalise_model(model: str) -> str:
    """Provider prefix (`provider/` or `provider:`) and date suffix stripped, so
    `anthropic/claude-x-20250101` == `anthropic:claude-x` == `claude-x`."""
    return _DATE_SUFFIX.sub("", re.split(r"[/:]", model)[-1].lower())


def model_flags(model: str | None, actual: Sequence[str], unobserved: bool) -> frozenset[TrialFlag]:
    """What the served models say about the requested one. `actual` holds only models a response
    named, so a request that no response answered with a model name is `unobserved`, never a match."""
    if model is None:
        return frozenset()
    flags: set[TrialFlag] = set()
    if unobserved or not actual:
        flags.add(TrialFlag.MODEL_UNOBSERVED)
    if any(normalise_model(a) != normalise_model(model) for a in actual):
        flags.add(TrialFlag.MODEL_MISMATCH)
    return frozenset(flags)


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


def tool_diagnostics(steps: Sequence[Step]) -> list[JsonValue]:
    """The harness tool steps every judge reads, from a live run or a saved trajectory alike."""
    return [s.model_dump(mode="json") for s in steps if s.kind is StepKind.TOOL_CALL]


def decide(
    na_reason: str | None,
    invalid_reasons: Sequence[str],
    status: TrajectoryStatus | None,
    checks: Sequence[CheckResult],
    *,
    execution_valid: bool | None = None,
) -> tuple[Verdict, str | None]:
    """NA > INVALID > FAIL > PASS; returns the verdict and, for INVALID, its reason."""
    if na_reason is not None:
        return Verdict.NA, None
    missing = [
        c.detail if c.detail.startswith(ENVIRONMENT_INTERRUPTION) else f"{c.id}: {c.detail}"
        for c in checks
        if c.passed is None and not c.advisory
    ]
    reasons = [*invalid_reasons, *missing]
    if execution_valid is False:
        reasons.insert(0, "semantic judge rejected execution validity")
    if status is TrajectoryStatus.BOOT_ERROR:
        reasons.insert(0, "boot_error")
    if reasons:
        return Verdict.INVALID, "; ".join(reasons)
    match status:
        case TrajectoryStatus.HARNESS_ERROR | TrajectoryStatus.TIMEOUT | TrajectoryStatus.OVER_BUDGET:
            if execution_valid is not True:
                return Verdict.INVALID, f"execution incomplete: {status.value}; attribution requires semantic judging"
        case TrajectoryStatus.COMPLETED:
            pass
        case TrajectoryStatus.BOOT_ERROR | None:
            raise ValueError(f"a graded trial needs a run status, got {status}")
        case _:
            assert_never(status)
    gates_pass = all(c.passed is True for c in checks if c.gate and not c.advisory)
    return (Verdict.PASS if gates_pass else Verdict.FAIL), None


def score(checks: Sequence[CheckResult]) -> float:
    """Weighted fraction of non-advisory checks passed (partial credit)."""
    counted = [c for c in checks if not c.advisory]
    total = sum(c.weight for c in counted)
    if not counted:
        return 0.0
    if total <= 0:
        raise ValueError("non-advisory check weights sum to zero; partial credit is undefined")
    return sum(c.weight for c in counted if c.passed is True) / total


# ---------------------------------------------------------------------------
# in-process servers (host / remote isolation)
# ---------------------------------------------------------------------------


class _QuietServer(uvicorn.Server):
    # The runner owns Ctrl-C; an embedded server must not swallow it.
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


@dataclass
class _Embedded:
    server: _QuietServer
    task: asyncio.Task[None]
    port: int

    async def stop(self) -> None:
        self.server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await self.task


async def serve_embedded(app: ASGIApp, host: str, port: int = 0) -> _Embedded:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise BoxError(f"cannot serve on {host}:{port} (is the port taken?): {exc.strerror}") from exc
    port = int(sock.getsockname()[1])
    server = _QuietServer(uvicorn.Config(app, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            task.result()
            raise BoxError(f"embedded server on {host}:{port} exited during startup")
        await asyncio.sleep(0.01)
    return _Embedded(server=server, task=task, port=port)


def world_bind_host(public_url: str | None) -> str:
    return "0.0.0.0" if public_url else "127.0.0.1"


def advertised_world_mcp_url(public_url: str | None, world_local: str) -> str:
    return f"{public_url.rstrip('/')}/mcp" if public_url is not None else f"{world_local.rstrip('/')}/mcp"


@dataclass
class Services:
    """The run's world server and (when any harness is metered) its meter, wherever isolation puts them."""

    isolation: Isolation
    public_url: str | None
    need_meter: bool
    run_id: str
    egress_hosts: tuple[str, ...] = ()
    world_port: int | None = None
    world_admin: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    meter_admin: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    world_local: str = ""  # runner-side base URL of the world server
    world_internal: str = ""  # MCP URL as a docker box sees it
    meter_local: str | None = None
    meter_internal: str | None = None
    egress_internal: str | None = None  # the docker sidecar's proxy to the model endpoints
    network: str | None = None
    _server: WorldServer | None = None
    _sidecar: Sidecar | None = None
    _embedded: list[_Embedded] = field(default_factory=list)

    async def start(self) -> None:
        match self.isolation:
            case Isolation.DOCKER:
                self._sidecar = Sidecar(self.world_admin, self.meter_admin, self.egress_hosts)
                urls = await self._sidecar.up(self.run_id)
                self.network = self._sidecar.network
                self.world_local = urls.world_host.removesuffix("/mcp")
                self.world_internal = urls.world_internal
                self.meter_local, self.meter_internal = urls.meter_host, urls.meter_internal
                self.egress_internal = urls.egress_internal
            case Isolation.HOST | Isolation.REMOTE:
                await self._start_embedded()
            case _:
                assert_never(self.isolation)

    async def _start_embedded(self) -> None:
        self._server = WorldServer(admin_token=self.world_admin)
        world, _url = await self.serve_embedded_world(self._server.app(), self.world_port or 0)
        self._embedded.append(world)
        self.world_local = f"http://127.0.0.1:{world.port}"
        if self.need_meter:
            meter = await serve_embedded(meter_app(self.meter_admin, upstreams_from_env(os.environ)), "127.0.0.1")
            self._embedded.append(meter)
            self.meter_local = self.meter_internal = f"http://127.0.0.1:{meter.port}"

    async def serve_embedded_world(self, app: ASGIApp, port: int = 0) -> tuple[_Embedded, str]:
        """Serve a world app with the public bind, URL and reachability contract for this run."""
        embedded = await serve_embedded(app, world_bind_host(self.public_url), port)
        local = f"http://127.0.0.1:{embedded.port}"
        if self.public_url is not None:
            await check_public_world(self.public_url, local)
        return embedded, advertised_world_mcp_url(self.public_url, local)

    async def stop(self) -> None:
        for embedded in reversed(self._embedded):
            await embedded.stop()
        self._embedded.clear()
        if self._sidecar is not None:
            await self._sidecar.down()

    async def register(self, task: Task) -> tuple[str, str]:
        if self._server is not None:
            return self._server.register(World.for_task(task))
        # The sidecar cannot see the host's task directories, so the loaded task travels whole.
        reg = await admin_register(self.world_local, self.world_admin, task)
        return reg.token, reg.control_token

    async def register_tau2(self, backend: Any) -> tuple[str, str, str, str] | None:
        """Register a tau2 backend on the listener the public URL already reaches."""
        if self._server is None:
            return None
        token, control_token, nonce = self._server.register_backend(backend)
        try:
            if self.public_url is not None:
                await check_public_world(self.public_url, self.world_local, token=token, nonce=nonce)
        except BaseException:
            self._server.unregister(control_token)
            raise
        url = advertised_world_mcp_url(self.public_url, self.world_local)
        return token, control_token, nonce, url

    def unregister_backend(self, control_token: str) -> None:
        if self._server is not None:
            self._server.unregister(control_token)

    def control(self, control_token: str) -> ControlClient:
        return ControlClient(self.world_local, control_token)

    def mcp_url(self, harness_isolation: Isolation, box_isolation: Isolation, *, uses_world: bool = True) -> str:
        """`uses_world`: the task mounts the world (`Capability.MCP`). A remote product reaches it only at
        the public URL; a task that does not mount it (a platform task) needs none, and gets the box's."""
        if harness_isolation is Isolation.REMOTE and uses_world:
            if self.public_url is None:
                raise HarnessError("this harness reaches the world remotely: pass --world-public-url")
            return f"{self.public_url.rstrip('/')}/mcp"
        match box_isolation:
            case Isolation.DOCKER:
                return self.world_internal
            case Isolation.HOST | Isolation.REMOTE:
                return f"{self.world_local}/mcp"
            case _:
                assert_never(box_isolation)

    def egress_env(self, task: Task, box_isolation: Isolation) -> dict[str, str]:
        """A harness-launched desktop task's harness calls its model provider itself, so a docker box gets the
        sidecar's allowlisting proxy to the model endpoints, the only way out of its internal network."""
        desktop = task.meta.desktop
        if desktop is None or desktop.launch_by != "harness" or box_isolation is not Isolation.DOCKER:
            return {}
        if self.egress_internal is None:
            raise BoxError("a harness-launched desktop task needs the sidecar's egress proxy, which this run lacks")
        no_proxy = "sidecar,localhost,127.0.0.1"
        return {
            "HTTPS_PROXY": self.egress_internal, "https_proxy": self.egress_internal,
            "NO_PROXY": no_proxy, "no_proxy": no_proxy,
        }  # fmt: skip

    def meter_for_box(self, box_isolation: Isolation) -> str:
        url = self.meter_internal if box_isolation is Isolation.DOCKER else self.meter_local
        if url is None:
            raise BoxError("a metered harness needs the meter, which this run did not start")
        return url

    def meter_client(self) -> MeterClient:
        if self.meter_local is None:
            raise BoxError("a metered harness needs the meter, which this run did not start")
        return MeterClient(self.meter_local, self.meter_admin)

    async def trial_network(self, trial_id: str) -> str:
        if self._sidecar is None:
            raise BoxError("no-network TB2 tasks need the Docker sidecar network")
        return await self._sidecar.trial_network(trial_id)

    async def remove_trial_network(self, network: str) -> None:
        if self._sidecar is not None:
            await self._sidecar.remove_trial_network(network)


PUBLIC_WORLD_WAIT_S = 60.0  # a fresh quick tunnel's hostname can take some seconds to resolve


async def check_public_world(
    public_url: str, world_local: str, *, token: str | None = None, nonce: str | None = None
) -> None:
    """The public URL must reach this run's world server: its `/mcp` answers exactly as the local one.
    A tunnel routed elsewhere would otherwise hand every remote trial an empty world."""
    path = "/_identity" if token is not None else "/mcp"
    public = f"{public_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with httpx.AsyncClient(timeout=10.0) as http:
        local = await http.get(f"{world_local}{path}", headers=headers)
        deadline = time.monotonic() + PUBLIC_WORLD_WAIT_S
        while True:
            try:
                seen = await http.get(public, headers=headers)
                same_identity = (
                    token is not None
                    and nonce is not None
                    and local.status_code == 200
                    and seen.status_code == 200
                    and local.json().get("nonce") == nonce
                    and seen.json().get("nonce") == nonce
                )
                if same_identity or (
                    token is None and (seen.status_code, seen.content) == (local.status_code, local.content)
                ):
                    return
                answer = f"HTTP {seen.status_code}"
            except httpx.TransportError as exc:
                answer = f"{type(exc).__name__}"
            if time.monotonic() > deadline:
                raise BoxError(f"--world-public-url {public} does not reach this run's world server ({answer})")
            await asyncio.sleep(1.0)


@contextlib.asynccontextmanager
async def services(
    isolation: Isolation,
    public_url: str | None,
    need_meter: bool,
    run_id: str,
    world_port: int | None = None,
    egress_hosts: Sequence[str] = (),
) -> AsyncGenerator[Services]:
    svc = Services(
        isolation=isolation,
        public_url=public_url,
        need_meter=need_meter,
        run_id=run_id,
        world_port=world_port,
        egress_hosts=tuple(egress_hosts),
    )
    try:
        await svc.start()
        yield svc
    finally:
        await finish_uncancelled(svc.stop())  # a Ctrl-C landing mid-teardown would strand the run network


# ---------------------------------------------------------------------------
# one trial
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessPlan:
    name: str
    info: HarnessInfo
    version: str | None  # None: the version probe failed, see `unavailable`
    config_hash: str
    unavailable: str | None = None
    # A box kind whose probe failed while another succeeded: only that kind's trials are INVALID,
    # so an optional image (the desktop's is amd64-only) cannot void the rest of the run.
    unavailable_boxes: Mapping[BoxKind, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TrialPlan:
    task: Task
    harness: HarnessPlan
    model: str | None
    repeat: int
    key: str
    attempt: int = 0  # the physical attempt: 0 the first, then one more per retry or resume of an INVALID row

    @property
    def identity(self) -> tuple[str, str, str | None, int]:
        return self.task.id, self.harness.name, self.model, self.repeat

    @property
    def rel_dir(self) -> str:
        return trial_rel_dir(self.task.id, self.harness.name, self.model, self.repeat, self.attempt)


@dataclass(frozen=True)
class TrialRun:
    result: TrialResult
    evidence: Evidence | None
    # What grading the trial cost in USD; None when a judge call had no price.
    judge_cost_usd: float | None = 0.0

    def spent(self) -> float | None:
        """The trial's whole spend, the harness's and its judges'; None when either is unknown. A validity control
        runs no model, so it spends nothing, though its row shows its cost as unreported rather than $0."""
        r = self.result
        harness = 0.0 if is_validity_control(r.harness_kind, r.model) else r.cost_usd
        if harness is None or self.judge_cost_usd is None:
            return None
        return harness + self.judge_cost_usd


@dataclass
class Transcript:
    answers: list[str]
    delivered_turns: list[TurnInput] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)
    actual_models: list[str] = field(default_factory=list)
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED
    error: str | None = None
    ttft_ms: int | None = None
    boot_ms: int = 0
    notes: list[str] = field(default_factory=list)
    extra: JsonObject = field(default_factory=dict)
    turn_latency_ms: list[int] = field(default_factory=list)
    turn_split: list[LatencySplit | None] = field(default_factory=list)
    flags: set[TrialFlag] = field(default_factory=set)  # reported by the harness
    approvals: list[ApprovalAnswer] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)  # reported by the harness
    timing: Timing | None = None  # reported by the harness (`protocols.TrialTiming`)
    costs: dict[str, float | None] = field(default_factory=dict)  # reported by the harness (`protocols.TrialCosts`)
    details: JsonObject = field(default_factory=dict)  # reported by the harness (`protocols.TrialDetails`)

    @property
    def interrupted(self) -> bool:
        """The harness reported that the environment, not the agent, ended the trial (`ENVIRONMENT_INTERRUPTION`)."""
        return (
            self.status is TrajectoryStatus.HARNESS_ERROR
            and self.error is not None
            and self.error.startswith(ENVIRONMENT_INTERRUPTION)
        )

    @property
    def agent_ms(self) -> int | None:
        return sum(self.turn_latency_ms) if self.turn_latency_ms else None

    @property
    def latency_split(self) -> LatencySplit | None:
        return summed_split(self.turn_split)


def summed_split(turns: Sequence[LatencySplit | None]) -> LatencySplit | None:
    """A trial's split: its turns' summed per field; a field any turn lacks is unknown, not partial."""
    if not turns or any(t is None for t in turns):
        return None
    known = [t for t in turns if t is not None]

    def total(values: list[int | None]) -> int | None:
        return None if None in values else sum(v or 0 for v in values)

    return LatencySplit(
        server_ms=total([t.server_ms for t in known]),
        answer_ms=total([t.answer_ms for t in known]),
        exit_ms=total([t.exit_ms for t in known]),
        settle_ms=total([t.settle_ms for t in known]),
    )


def harness_isolation(info: HarnessInfo, run_isolation: Isolation) -> Isolation:
    """Controls always run on the host and remote-only harnesses remotely; the rest follow the run."""
    match info.isolation:
        case Isolation.HOST | Isolation.REMOTE:
            return info.isolation
        case Isolation.DOCKER:
            return run_isolation
        case _:
            assert_never(info.isolation)


def trial_capabilities(info: HarnessInfo, run_isolation: Isolation) -> frozenset[Capability]:
    """What a trial can rely on: the harness's capabilities, plus the world's clock where it holds.
    A docker box runs the harness under px-clock, and a control (always on the host) reads no clock.
    An agent run with `--isolation host` reads the machine's real date, and a remote product keeps
    its own clock, so neither has it unless the harness declares `world_clock`."""
    caps = info.capabilities
    # A control writes the desktop app's evidence itself; an agent needs the box's desktop session to reach it.
    if info.kind is not HarnessKind.CONTROL and not desktop_box(info, harness_isolation(info, run_isolation)):
        caps -= {Capability.DESKTOP}
    match harness_isolation(info, run_isolation):
        case Isolation.DOCKER:
            caps |= {Capability.WORLD_CLOCK}
        case Isolation.HOST if info.isolation is Isolation.HOST:
            caps |= {Capability.WORLD_CLOCK}
        case _:
            pass
    return caps


def trial_web_isolation(info: HarnessInfo, isolation: Isolation) -> WebIsolation:
    """A remote harness has no egress the runner controls, but one whose adapter switches its native
    web off (`web_disabled_by`) is as isolated as a host harness doing the same by flag or config."""
    web = web_isolation_for(isolation)
    if web is WebIsolation.NONE and info.web_disabled_by != "none":
        return WebIsolation.FLAGS
    return web


def box_isolation(box: Box) -> Isolation:
    """Where the box's processes run: a device box is a container serving a remote harness."""
    return box.isolation


def final_message(answer: str, steps: Sequence[Step]) -> str:
    """A turn's closing message: the run of message steps after the turn's last tool step that
    the answer ends with. Adapters build the answer with `turn_answer` over message texts, so the
    run is the text written after the last tool call, even when it spans several text blocks;
    requiring the suffix skips message steps that are not assistant text (CLI notices, a remote
    product's status lines, sub-agent text). An adapter that reports no such step (the oracle,
    a remote product's final message, hermes) delivers the reply as a single message, so the answer is it."""
    body = answer.strip()
    run: list[str] = []
    for s in reversed(steps):
        if s.kind is not StepKind.MESSAGE:
            if run:
                break
            continue
        text = (s.output or "").strip()
        if text and body.endswith(turn_answer([text, *run])):
            run.insert(0, text)
    return turn_answer(run) if run else body


def turn_finals(answers: Sequence[str], steps: Sequence[Step]) -> tuple[str, ...]:
    """The one producer of `Evidence.finals`, for live trials and saved trajectories alike."""
    return tuple(final_message(a, [s for s in steps if s.turn == i]) for i, a in enumerate(answers, start=1))


def turn_status(status: TurnStatus) -> TrajectoryStatus:
    match status:
        case TurnStatus.COMPLETED:
            return TrajectoryStatus.COMPLETED
        case TurnStatus.HARNESS_ERROR:
            return TrajectoryStatus.HARNESS_ERROR
        case TurnStatus.TIMEOUT:
            return TrajectoryStatus.TIMEOUT
        case _:
            assert_never(status)


def fire_text(fire: Fire, job: JsonObject, tz: str) -> str:
    at = fire.at.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")
    return FIRE_TEXT.format(title=job.get("title", ""), at=at, instruction=job.get("instruction", ""))


class Session:
    """What every trial of one run shares: the services, the judge and where results go."""

    def __init__(
        self,
        svc: Services,
        run_dir: Path,
        panel: JudgePanel | None,
        judge_config: JudgeConfig | None,
        turn_timeout_s: float | None = None,
        budget: CampaignBudget | None = None,
        videos: Path | None = None,
        run_id: str | None = None,
    ) -> None:
        self.svc = svc
        self.run_dir = run_dir
        self.run_id = run_id or uuid.uuid4().hex  # labels every task container this run starts
        self.videos = videos
        self.panel = panel
        self.judge_config = judge_config
        self.turn_timeout_s = turn_timeout_s
        self.budget = budget
        self.live: LiveFeed | None = None

    async def trial(self, plan: TrialPlan) -> TrialRun:
        task, hp = plan.task, plan.harness
        missing = task.meta.requires - trial_capabilities(hp.info, self.svc.isolation)
        if missing:
            na = f"requires {', '.join(sorted(c.value for c in missing))}"
            return TrialRun(self.unrun(plan, na=na), None)
        missing_secrets = missing_task_secret_env(task)
        if missing_secrets:
            alias, env_name = missing_secrets[0]
            return TrialRun(
                self.unrun(plan, invalid=f"missing secret environment variable {env_name} for {alias}"), None
            )
        if hp.unavailable is not None:
            return TrialRun(self.unrun(plan, invalid=f"harness unavailable: {hp.unavailable}"), None)
        kind = box_kind(task.meta.requires, hp.info, harness_isolation(hp.info, self.svc.isolation))
        if kind in hp.unavailable_boxes:
            return TrialRun(self.unrun(plan, invalid=f"{kind} box unavailable: {hp.unavailable_boxes[kind]}"), None)
        if self.budget is None:
            return await self.execute(plan)
        if hp.info.cost_source is CostSource.METERED and plan.model is not None:
            wire = trial_wire(plan.model, hp.info.meter_wire)
            upstream_url = upstreams_from_env(os.environ)[wire].base_url
            if not model_price_known(wire_model_name(plan.model), upstream_url):
                return TrialRun(
                    self.unrun(
                        plan,
                        invalid=f"infrastructure error: no known price for budgeted model {plan.model!r}",
                    ),
                    None,
                )
        try:
            hold = await self.budget.reserve(trial_name(plan))
        except BudgetRefused as exc:
            return TrialRun(self.unrun(plan, invalid=f"budget refused: {exc}"), None)
        spent: float | None = None
        try:
            run = await self.execute(plan)
            spent = run.spent()
            return run
        finally:
            await finish_uncancelled(self.budget.settle(hold, spent))

    async def execute(self, plan: TrialPlan) -> TrialRun:
        trial_dir = self.run_dir / plan.rel_dir
        # An attempt with no row (the run was interrupted inside it) reruns from scratch: a leftover workspace would
        # merge into the new snapshot. A recorded attempt has a directory of its own, which is never reused.
        shutil.rmtree(trial_dir, ignore_errors=True)
        trial_dir.mkdir(parents=True)
        return await TrialExecution(self, plan, trial_dir).run()

    def unrun(self, plan: TrialPlan, *, na: str | None = None, invalid: str | None = None) -> TrialResult:
        hp = plan.harness
        verdict, reason = decide(na, [invalid] if invalid else [], None if na else TrajectoryStatus.BOOT_ERROR, [])
        return result_row(
            plan, verdict=verdict, invalid_reason=reason, na_reason=na, status=None if na else TrajectoryStatus.BOOT_ERROR,
            checks=(), usage=(), cost_source=hp.info.cost_source, actual=(), unobserved=True,
            timings=(None, None, None, None),
            judge=(), same_family=False, web=trial_web_isolation(hp.info, harness_isolation(hp.info, self.svc.isolation)),
            rel_dir=None,
        )  # fmt: skip


def result_row(
    plan: TrialPlan,
    *,
    verdict: Verdict,
    invalid_reason: str | None,
    na_reason: str | None,
    status: TrajectoryStatus | None,
    checks: tuple[CheckResult, ...],
    usage: tuple[Usage, ...],
    cost_source: CostSource,
    actual: tuple[str, ...],
    unobserved: bool,
    timings: tuple[int | None, int | None, int | None, int | None],  # boot, ttft, wall, agent
    judge: tuple[str, ...],
    same_family: bool,
    web: WebIsolation,
    rel_dir: str | None,
    harness_flags: frozenset[TrialFlag] = frozenset(),
    split: LatencySplit | None = None,
    metrics: dict[str, float] | None = None,
    reported: Transcript | None = None,
    judge_identity: str | None = None,
    judged_score: float | None = None,
) -> TrialResult:
    """`reported`: the transcript whose harness-reported timing, cost components and details the row carries.
    `judged_score`: a published judge's own score, which replaces the checks' weighted fraction."""
    task, hp = plan.task, plan.harness
    boot, ttft, wall, agent = timings
    # A harness that reports its cost by component reports all of it: the components are the trial's cost, unknown
    # when any one is. Otherwise the cost is the usage's.
    components = reported.costs if reported is not None else {}
    costs = list(components.values()) if components else [u.cost_usd for u in usage]
    timing = reported.timing if reported is not None else None
    flags = model_flags(plan.model, actual, unobserved) | harness_flags
    if hp.info.credential_env:
        flags |= {TrialFlag.NATIVE_CREDENTIAL}
    return TrialResult.model_validate(
        {
            "key": plan.key, "task_id": task.id, "task_version": task.meta.version, "task_hash": task.hash,
            "harness": hp.name, "harness_version": hp.version or "unavailable", "harness_kind": hp.info.kind,
            "config_hash": hp.config_hash, "model": plan.model, "actual_models": actual,
            "repeat": plan.repeat, "verdict": verdict, "xfail": task.meta.xfail is not None,
            "score": judged_score if judged_score is not None else score(checks) if checks else 0.0, "invalid_reason": invalid_reason, "na_reason": na_reason,
            "status": status, "checks": checks, "usage": usage,
            "cost_usd": None if not costs or any(c is None for c in costs) else sum(c for c in costs if c is not None),
            "cost_source": cost_source, "boot_ms": boot, "ttft_ms": ttft, "wall_ms": wall, "agent_ms": agent,
            "latency_split": split, "judge": judge,
            "judge_same_family": same_family, "web_isolation": web, "maturity": task.meta.maturity,
            "split": task.meta.split, "area": task.meta.area, "lite": task.id in members(LITE), "dir": rel_dir,
            "flags": flags, "metrics": metrics or {}, "attempt": plan.attempt,
            "timing_basis": timing.basis if timing is not None else None,
            "timing_breakdown": timing.breakdown if timing is not None else {},
            "cost_by_component": {k: v for k, v in components.items() if v is not None}, "details": reported.details if reported is not None else {},
            "judge_identity": judge_identity,
        }
    )  # fmt: skip


def desktop_commands(task: Task) -> tuple[tuple[str, ...], ...]:
    """The apps a harness starts itself: the box opens none when `launch_by = "harness"`."""
    desktop = task.meta.desktop
    return tuple(app.command for app in desktop.apps) if desktop is not None and desktop.launch_by == "harness" else ()


class TrialExecution:
    """Every step of one trial."""

    def __init__(self, session: Session, plan: TrialPlan, trial_dir: Path) -> None:
        self.s = session
        self.plan = plan
        self.task = plan.task
        self.dir = trial_dir
        n_turns = len(self.task.turns) + len(self.task.meta.fires)
        self.t = Transcript(answers=[""] * n_turns)
        # Browser judges need the same reference date as the run even when the task uses natural language.
        # Placeholder expansion remains limited to explicit date placeholders, while world tasks keep their
        # existing clock semantics.
        self.today = (
            datetime.now(UTC).date().isoformat()
            if self.task.meta.area is Area.BROWSER or any(DATE_PLACEHOLDER.search(t.text) for t in self.task.turns)
            else None
        )
        self.t.details = {"today": self.today} if self.today is not None else {}
        self.secrets: list[str] = [
            os.environ[env_name] for env_name in self.task.secrets.values() if os.environ.get(env_name)
        ]
        self.invalid: list[str] = []
        self.meter_key: str | None = None
        self.box_iso = harness_isolation(plan.harness.info, session.svc.isolation)
        self.snapshot_turns = frozenset(
            c.turn for c in self.task.checks.checks if isinstance(c, FileCheck) and c.turn is not None
        )
        self.turn_workspaces: dict[int, Path] = {}
        self.last_turn = 0
        self.raw_output = ""
        self.product: JsonObject | None = None  # the harness's `trial_metadata`, for a grading policy
        self.tau2_recorded: JsonObject | None = None  # parallax-recorded tau2 transcript, never harness-reported
        self.tau2_mcp: McpEndpoint | None = None  # the trial's tau2 tool server, parallax-served
        self._tau2_runtime: Tau2BridgeRuntime | None = None  # the trial's tau2 serve process
        self._tau2_log: Tau2ToolLog | None = None  # its tool log
        self.tau2_token: str | None = None
        self._tau2_backend_control: str | None = None
        self.task_container: str | None = None  # parallax-owned task container, removed after grading
        self.task_network: str | None = None  # per-trial network for no-network TB2 tasks
        self.assets = TrialAssets(self.dir)

    async def run(self) -> TrialRun:
        try:
            started = time.monotonic()
            evidence: Evidence | None = None
            meter: MeterReport | None = None
            harness = await self.prepare()
            if harness is None:
                return TrialRun(self.finish(started, None), None)
            if self.task.seed is None:
                return await self.run_worldless(started, harness)
            try:
                token, control_token = await self.s.svc.register(self.task)
            except (WorldError, ControlError, httpx.HTTPError, OSError, ValueError) as exc:
                self.invalid.append(f"world registration failed: {str(exc) or type(exc).__name__}")
                await self.stop_unstarted(harness)
                return TrialRun(self.finish(started, None), None)
            self.secrets += [token, control_token]
            async with self.s.svc.control(control_token) as control:
                try:
                    box = await open_box(self.task, self.plan.harness.info, self.box_iso, self.s.svc.network)
                except (BoxError, HarnessError, OSError) as exc:
                    self.t.status, self.t.error = TrajectoryStatus.BOOT_ERROR, f"box: {exc}"
                    await self.stop_unstarted(harness)
                    await self.unregister(control)
                    return TrialRun(self.finish(started, None), None)
                try:
                    await self.drive(harness, box, token, control)
                    workspace = await self.snapshot(box)
                finally:
                    await self.keep_recording(box)
                    await box.close()
                world = await self.world_evidence(control)
                meter = await self.meter_report()
                await self.unregister(control)
            if world is not None:
                verify = await self.verify(workspace)
                evidence = Evidence(
                    delivered_turns=tuple(self.t.delivered_turns),
                    answers=tuple(self.t.answers), finals=turn_finals(self.t.answers, self.t.steps),
                    world=world, workspace=workspace,
                    turn_workspaces=self.turns_after(workspace),
                    seed_files=seed_dir(self.task) if seed_dir(self.task).is_dir() else None,
                    verify=verify, persona_tz=self.task.world.user.timezone, approvals=tuple(self.t.approvals),
                )  # fmt: skip
            return await self.graded(started, evidence, meter)
        finally:
            await finish_uncancelled(self.revoke_meter())

    async def run_worldless(self, started: float, harness: Harness) -> TrialRun:
        """A task with no world: no world is registered or mounted, and its policy grades what the harness reported."""
        try:
            box = await open_box(self.task, self.plan.harness.info, self.box_iso, self.s.svc.network)
        except (BoxError, HarnessError, OSError) as exc:
            self.t.status, self.t.error = TrajectoryStatus.BOOT_ERROR, f"box: {exc}"
            await self.stop_unstarted(harness)
            return TrialRun(self.finish(started, None), None)
        if Capability.LIVE_CONTAINER in self.task.meta.requires:
            from parallax.harbor_tasks import task_container_name

            # Recorded before `docker run`, so the finally below removes the container even
            # when starting it fails or the trial is cancelled mid-start.
            self.task_container = task_container_name()
        try:
            if self.task_container is not None:
                from parallax.harbor_tasks import HarborTaskError, start_task_container

                try:
                    from parallax.harbor_tasks import task_network_mode

                    if task_network_mode(self.task.data) == "no-network":
                        self.task_network = await self.s.svc.trial_network(self.trial_id())
                    await start_task_container(
                        self.task.data,
                        name=self.task_container,
                        trial_id=self.trial_id(),
                        run_id=self.s.run_id,
                        network=self.task_network or self.s.svc.network,
                        egress_proxy=self.s.svc.egress_internal,
                    )
                except (HarborTaskError, OSError) as exc:
                    # The same cleanup as every other early return: stop the unstarted
                    # harness and close the box; the finally removes the container.
                    self.t.status, self.t.error = TrajectoryStatus.BOOT_ERROR, f"task container: {exc}"
                    await self.stop_unstarted(harness)
                    await box.close()
                    return TrialRun(self.finish(started, None), None)
            if self.task.grading == "tau2-upstream-v1":
                return await self.run_tau2_worldless(started, harness, box)
            try:
                await self.drive(harness, box, None, None)
            finally:
                await self.keep_recording(box)
                await box.close()
            self.record_tb2_storage()
            return await self.graded(started, None, await self.meter_report())
        finally:
            try:
                if self.task_container is not None:
                    from parallax.harbor_tasks import remove_container

                    await remove_container(self.task_container)
            finally:
                if self.task_network is not None:
                    await finish_uncancelled(self.s.svc.remove_trial_network(self.task_network))

    async def run_tau2_worldless(self, started: float, harness: Harness, box: Box) -> TrialRun:
        """A tau2 trial: parallax starts the tool server and simulator, then drives the loop."""
        from parallax.official_suites import OfficialSuiteError, tau2_task_from_data
        from parallax.tau2_runtime import Tau2RuntimeError, Tau2ToolLog, tau2_mcp_app

        embedded = None
        try:
            try:
                task_json = tau2_task_from_data(self.task.data)
                self._tau2_runtime = self._make_tau2_runtime()
                await self._tau2_runtime.start(domain=str(self.task.data.get("domain", "")), task=task_json)
            except (OfficialSuiteError, Tau2RuntimeError, OSError, ValueError) as exc:
                self.t.status, self.t.error = TrajectoryStatus.BOOT_ERROR, f"tau2 runtime: {exc}"
                await self.stop_unstarted(harness)
                await box.close()
                return TrialRun(self.finish(started, None), None)
            self._tau2_log = Tau2ToolLog(self._tau2_runtime)
            self._tau2_backend_control: str | None = None
            from parallax.tau2_runtime import Tau2Backend

            registered = await self.s.svc.register_tau2(Tau2Backend(self._tau2_log))
            if registered is not None:
                token, control_token, _nonce, url = registered
                self._tau2_backend_control = control_token
                self.secrets.append(token)
                self.tau2_token = token
                self.tau2_mcp = McpEndpoint(url=url, stdio=())
            else:
                if self.s.svc.public_url is not None:
                    raise BoxError("tau2 public world requires the public URL to target the shared world listener")
                token = secrets.token_urlsafe(32)
                nonce = secrets.token_urlsafe(32)
                embedded, url = await self.s.svc.serve_embedded_world(
                    tau2_mcp_app(self._tau2_log, token=token, nonce=nonce)
                )
                self.secrets.append(token)
                self.tau2_token = token
                self.tau2_mcp = McpEndpoint(url=url, stdio=())
            try:
                await self.drive(harness, box, None, None)
            finally:
                await self.keep_recording(box)
                await box.close()
            self.record_tb2_storage()
            return await self.graded(started, None, await self.meter_report())
        finally:
            if embedded is not None:
                await embedded.stop()
            backend_control = self._tau2_backend_control
            if backend_control is not None:
                self.s.svc.unregister_backend(backend_control)
            if self._tau2_runtime is not None:
                await self._tau2_runtime.close()

    async def keep_recording(self, box: Box) -> None:
        """A desktop trial's video of its display, as `desktop.mp4` in its trial directory or the run's `videos`."""
        if not isinstance(box, DockerBox):
            return
        folder = self.dir if self.s.videos is None else self.s.videos / self.plan.rel_dir
        folder.mkdir(parents=True, exist_ok=True)
        await box.keep_recording(folder / "desktop.mp4")

    async def unregister(self, control: ControlClient) -> None:
        with contextlib.suppress(ControlError, httpx.HTTPError):
            await control.unregister()

    # ---- driving the harness ------------------------------------------------------------

    async def prepare(self) -> Harness | None:
        """The trial's harness, bound to its task. An `AccountBound` harness names its account first and
        the task is rebound to it, so everything downstream (world, turns, grading) sees one address.
        None, with the boot error recorded, when the harness cannot get that far."""
        harness: Harness | None = None
        try:
            harness = load_harness(self.plan.harness.name)
            if isinstance(harness, TaskBound):
                harness.bind_task(self.task)
            if isinstance(harness, AccountBound) and (email := await harness.account(self.trial_id())) is not None:
                self.task = rebind_persona(self.task, email)
                if isinstance(harness, TaskBound):
                    harness.bind_task(self.task)
        except Exception as exc:  # loading, binding or minting the account: the trial never got to start
            self.t.status, self.t.error = TrialExecution.boot_failed(exc)
            if harness is not None:
                await self.stop_unstarted(harness)
            return None
        return harness

    @staticmethod
    async def stop_unstarted(harness: Harness) -> None:
        """Release what `prepare` acquired (an `AccountBound` harness's account) when the trial never starts."""
        try:
            await finish_uncancelled(harness.stop())
        except Exception:
            logger.exception("teardown of a harness that never started failed")

    def trial_id(self) -> str:
        return trial_name(self.plan)

    def record_tb2_storage(self) -> None:
        """Expose Harbor's declared storage limit and Parallax's unenforced status in trial details."""
        storage_mb = self.task.data.get("storage_mb")
        if self.task_container is not None and storage_mb is not None:
            self.t.details.update({"storage_mb": storage_mb, "storage_enforced": False})

    async def drive(self, harness: Harness, box: Box, token: str | None, control: ControlClient | None) -> None:
        """`token` and `control` are None for a world-less task."""
        boot_start = time.monotonic()
        try:
            trial = await self.trial_input(box, token)
        except Exception as exc:  # metering setup: the trial never got to start
            self.t.status, self.t.error = TrialExecution.boot_failed(exc)
            await self.stop_unstarted(harness)
            return
        try:
            try:
                await harness.start(trial, box)
            except Exception as exc:  # any adapter failure to start is the trial's boot error
                self.t.status, self.t.error = TrialExecution.boot_failed(exc)
                return
            self.t.boot_ms = int((time.monotonic() - boot_start) * 1000)
            try:
                if self.tau2_mcp is not None:
                    await self.tau2_loop(harness)
                else:
                    await self.conversation(harness, box, control)
            except (ControlError, httpx.HTTPError) as exc:
                self.invalid.append(f"world control failed mid-trial: {exc}")
        finally:
            try:
                await finish_uncancelled(harness.stop())
            except Exception as exc:
                logger.exception("trial harness teardown failed")
                self.invalid.append(f"harness teardown failed: {type(exc).__name__}: {exc}")
            finally:
                if isinstance(harness, Caveated):
                    self.invalid += harness.trial_invalid
                    self.t.notes += harness.trial_notes
                if isinstance(harness, StderrTailed) and harness.stderr_tail:
                    self.t.notes.append(f"stderr tail:\n{harness.stderr_tail}")
                if isinstance(harness, TrialMetadata):
                    self.t.extra["harness_metadata"] = self.product = harness.trial_metadata
                if self.assets.validate():
                    self.invalid.extend(self.assets.diagnostics())
                if isinstance(harness, TrialMetrics):
                    self.t.metrics = dict(harness.trial_metrics)
                if isinstance(harness, TrialTiming):
                    self.t.timing = harness.trial_timing()
                if isinstance(harness, TrialCosts):
                    self.t.costs = dict(harness.trial_costs())
                if isinstance(harness, TrialDetails):
                    self.t.details = {
                        **harness.trial_details(),
                        **({"today": self.today} if self.today is not None else {}),
                    }
                if isinstance(harness, RawLogged):
                    self.raw_output = harness.output_tail.text()

    @staticmethod
    def boot_failed(exc: BaseException) -> tuple[TrajectoryStatus, str]:
        return TrajectoryStatus.BOOT_ERROR, f"start: {type(exc).__name__}: {exc}"

    async def trial_input(self, box: Box, token: str | None) -> Trial:
        """`token` is the world's, None for a world-less task: it gets no MCP, no persona, and the wall clock."""
        hp = self.plan.harness
        token = token or self.tau2_token
        env = {} if token is None else {TOKEN_ENV: token}
        env |= self.s.svc.egress_env(self.task, box_isolation(box))
        # Only harnesses the meter cannot route declare these; the rows are flagged unmetered.
        credentials = native_credentials(hp.info, os.environ)
        self.secrets += credentials.values()
        env |= credentials
        llm = None
        if hp.info.cost_source is CostSource.METERED:
            if self.task_container is not None and self.s.budget is None:
                raise HarnessError("metered TB2 trials require --budget and --reserve for a hard spend cap")
            self.meter_key = new_trial_key()
            self.secrets.append(self.meter_key)
            budget_usd = self.s.budget.reservation if self.s.budget is not None else None
            await self.s.svc.meter_client().register(
                self.meter_key,
                budget_usd=budget_usd,
                model=wire_model_name(self.plan.model) if self.plan.model else None,
                wire=trial_wire(self.plan.model, self.plan.harness.info.meter_wire) if self.plan.model else None,
            )
            svc = self.s.svc
            llm = llm_endpoint(svc.meter_for_box(box_isolation(box)), self.meter_key, svc.meter_for_box(Isolation.HOST))
        mcp = self.tau2_mcp  # a tau2 trial's tool server, served by parallax for this trial
        if token is not None and self.tau2_mcp is None:
            url = self.s.svc.mcp_url(
                hp.info.isolation, box_isolation(box), uses_world=Capability.MCP in self.task.meta.requires
            )
            mcp = McpEndpoint(
                url=url, stdio=("python", "-m", "parallax.world", "bridge", "--url", url, "--token-env", TOKEN_ENV),
                services=tuple(sorted(mounted_services(self.task))),
            )  # fmt: skip
        seed = self.task.seed
        return Trial(
            trial_id=self.trial_id(), task_id=self.task.id, model=self.plan.model, mcp=mcp,
            workspace=box.workspace, home=box.home, llm=llm, env=env,
            user=seed.user if seed is not None else None, now=seed.now if seed is not None else datetime.now(UTC),
            requires=self.task.meta.requires, assets=self.assets, desktop_commands=desktop_commands(self.task),
            task_container=self.task_container,
        )  # fmt: skip

    async def conversation(self, harness: Harness, box: Box, control: ControlClient | None) -> None:
        """`control` is None for a world-less task, whose turns move no world and which has no fires."""
        session = 1
        for turn in self.task.turns:
            if turn.index > 1 and turn.new_session:
                session += 1
            if control is not None:
                await control.position(turn.index, session)
                if turn.patch is not None:
                    await control.patch(turn.patch)
            text, attached = await self.attach(box, turn)
            delivered = await self.deliver(
                harness, turn.index, text, turn.new_session, attached, origin=turn.origin, approval=turn.approval
            )
            await self.snapshot_turn(box, turn.index)
            if not delivered:
                return
        if control is None:
            if self.task.meta.fires:
                raise ValueError(f"task {self.task.id} schedules fires but has no world to fire them")
            return
        for offset, fire in enumerate(self.task.meta.fires, start=1):
            index = len(self.task.turns) + offset
            session += 1
            await control.position(index, session)
            if fire.patch is not None:
                await control.patch(self.fire_patch(fire.patch))
            job = await control.fire(fire)  # advances the clock, then selects the job; misses are logged
            try:
                await box.set_clock(fire.at)  # the box's wall clock follows the world's
            except BoxError as exc:  # a harness on the wrong date would be graded on a broken premise
                self.invalid.append(f"box clock could not follow the world to {fire.at.isoformat()}: {exc}")
                return
            if job is None:
                continue
            # the scheduler's own record of the delivery, so a later `list_runs` sees it
            row = {"id": f"run-{fire.name}", "job": str(job.get("id")), "at": fire.at.isoformat(), "fire": fire.name}
            await control.patch(WorldPatch(insert={"scheduler.runs": [row]}))
            text = fire_text(fire, job, self.task.world.user.timezone)
            if not await self.deliver(harness, index, text, True, ()):
                return

    def _make_tau2_runtime(self):  # noqa: ANN202 - the bridge runtime type, imported lazily
        """The per-trial tau2 serve process: pinned checkout, simulator through the configured gateway."""
        from parallax.official_suites import TAU2_SIMULATOR_MODEL, TAU2_UPSTREAM_COMMIT, _tau2_root
        from parallax.tau2_runtime import Tau2BridgeRuntime

        root, python = _tau2_root()
        config = self.s.judge_config
        base_url = config.base_url if config is not None else os.environ.get("PARALLAX_JUDGE_BASE_URL", "")
        api_key = config.api_key if config is not None else os.environ.get("PARALLAX_JUDGE_API_KEY", "")
        return Tau2BridgeRuntime(
            root=root, python=python, commit=TAU2_UPSTREAM_COMMIT, base_url=base_url, api_key=api_key,
            simulator_model=TAU2_SIMULATOR_MODEL,
        )  # fmt: skip

    async def tau2_loop(self, harness: Harness) -> None:
        """Drive simulator -> harness turns until an upstream termination condition.

        The transcript is recorded here, from the tool server's log and the turns themselves;
        nothing the harness reports about the simulation is graded.
        """
        from parallax.tau2_runtime import Tau2RuntimeError
        from parallax.tau2_terms import Tau2Event, Tau2Progress, tau2_progress

        runtime, log = self._tau2_runtime, self._tau2_log
        if runtime is None or log is None:
            raise ValueError("tau2 loop without its runtime")
        transcript: list[JsonObject] = []

        if runtime.max_steps is None or runtime.max_errors is None:
            raise Tau2RuntimeError("tau2 runtime did not provide upstream limits")
        max_steps, max_errors = runtime.max_steps, runtime.max_errors

        def cleaned(messages: object, *, stop: bool = False) -> list[JsonObject]:
            out: list[JsonObject] = []
            for message in messages if isinstance(messages, list) else []:
                if not isinstance(message, dict):
                    continue
                kept = {
                    k: message[k]
                    for k in (
                        "role",
                        "content",
                        "tool_calls",
                        "id",
                        "requestor",
                        "error",
                        "tau2_actor",
                        "tau2_batch",
                    )
                    if k in message
                }
                out.append(kept)  # fmt: skip
            if stop and out:
                out[-1]["tau2_stop"] = True
            return out

        def progress() -> Tau2Progress:
            batch_error_counts: dict[tuple[str, int], int] = {}
            for message in transcript:
                actor = message.get("tau2_actor")
                batch = message.get("tau2_batch")
                if actor == "env" and isinstance(batch, int) and batch > 0:
                    requestor = message.get("requestor")
                    key = (str(requestor) if requestor in {"assistant", "user"} else "assistant", batch)
                    batch_error_counts[key] = batch_error_counts.get(key, 0) + int(bool(message.get("error")))
            events: list[Tau2Event] = []
            emitted_batches: set[tuple[str, int]] = set()
            for message in transcript:
                actor = message.get("tau2_actor")
                if actor not in {"user", "agent", "env"}:
                    actor = {"user": "user", "tool": "env"}.get(str(message.get("role")), "agent")
                batch = message.get("tau2_batch")
                if actor == "env" and isinstance(batch, int) and batch > 0:
                    requestor = message.get("requestor")
                    key = (str(requestor) if requestor in {"assistant", "user"} else "assistant", batch)
                    if key in emitted_batches:
                        continue
                    emitted_batches.add(key)
                    error_count = batch_error_counts.get(key, 0)
                else:
                    error_count = int(bool(message.get("error"))) if actor == "env" else 0
                events.append(
                    Tau2Event(
                        actor=cast(Any, actor),
                        waits_for_environment=bool(message.get("tool_calls")),
                        stop=bool(message.get("tau2_stop")),
                        error=bool(error_count),
                        error_count=error_count,
                    )
                )
            return tau2_progress(events, max_steps, max_errors)

        termination: str | None = None
        try:
            first = await runtime.user_turn(first=True, remaining_steps=runtime.max_steps)
        except Tau2RuntimeError:
            first, termination = {"messages": []}, "user_error"
        transcript += cleaned(first.get("messages"), stop=bool(first.get("stop")))
        if termination is None:
            termination = progress().termination
        policy_prompt = self.task.turns[0].text if self.task.turns else ""
        index = 0
        seen_tools = 0
        while termination is None:
            index += 1
            user_text = next((str(m.get("content") or "") for m in reversed(transcript) if m.get("role") == "user"), "")
            while len(self.t.answers) < index:
                self.t.answers.append("")
            text = f"{policy_prompt}\n\nCustomer: {user_text}" if index == 1 else user_text
            delivered = await self.deliver(harness, index, text, False, ())
            new_entries = log.entries[seen_tools:]
            seen_tools = len(log.entries)
            if new_entries:
                by_batch: dict[int, list[JsonObject]] = {}
                for entry in new_entries:
                    batch_value = entry.get("batch")
                    batch = batch_value if isinstance(batch_value, int) else 0
                    by_batch.setdefault(batch, []).append(entry)
                for batch, entries in by_batch.items():
                    transcript.append({
                        "role": "assistant", "content": None, "tau2_actor": "agent", "tau2_batch": batch,
                        "tool_calls": [
                            {
                                "id": str(e["id"]),
                                "name": str(e["name"]),
                                "arguments": cast(JsonObject, e["arguments"]),
                                "requestor": "assistant",
                            }
                            for e in entries
                        ],
                    })  # fmt: skip
                    transcript += [
                        {
                            "role": "tool",
                            "id": str(e["id"]),
                            "content": str(e["content"]),
                            "requestor": "assistant",
                            "error": bool(e["error"]),
                            "tau2_actor": "env",
                            "tau2_batch": batch,
                        }
                        for e in entries
                    ]
            answer = self.t.answers[index - 1] if index - 1 < len(self.t.answers) else ""
            if delivered:
                transcript.append(
                    {
                        "role": "assistant",
                        "content": answer,
                        "tau2_actor": "agent",
                        "tau2_stop": "###STOP###" in answer,
                    }
                )
            current = progress()
            termination = current.termination
            if termination is None and not delivered:
                termination = "agent_error"
            if termination is None:
                try:
                    nxt = await runtime.user_turn(
                        first=False, assistant=answer, remaining_steps=runtime.max_steps - current.step_count
                    )
                except Tau2RuntimeError:
                    termination = "user_error"
                else:
                    messages = nxt.get("messages")
                    transcript += cleaned(messages, stop=bool(nxt.get("stop")))
                    termination = progress().termination
                    if termination is None and not messages:
                        termination = "max_steps" if progress().step_count >= max_steps else "user_error"
        if runtime.simulator_cost_usd is not None:
            self.t.metrics["simulator_cost_usd"] = runtime.simulator_cost_usd
        self.tau2_recorded = cast(
            JsonObject, {"transcript": transcript, "termination_reason": termination or "agent_error"}
        )
        self.t.extra["tau2_recorded"] = self.tau2_recorded

    def fire_patch(self, name: str) -> WorldPatch:
        """A fire's patch, read from the task directory, under the trial's persona (`rebind_persona`)."""
        patch = load_patch(self.task.path, name)
        seeded = self.plan.task.world.user.email
        return WorldPatch.model_validate(
            rebind_value(patch.model_dump(mode="json"), seeded, self.task.world.user.email)
        )

    async def attach(self, box: Box, turn: Turn) -> tuple[str, tuple[str, ...]]:
        rels: list[str] = []
        for att in turn.attachments:
            name = Path(att.path).name
            data = (seed_dir(self.task) / att.path).read_bytes()
            await box.write_file(box.workspace / "attachments" / name, data)
            rels.append(f"attachments/{name}")
        text = turn.text if self.today is None else fill_dates(turn.text, date.fromisoformat(self.today))
        if not rels:
            return text, ()
        lines = "\n".join(f"Attached: {r}" for r in rels)
        return f"{text.rstrip()}\n\n{lines}\n", tuple(rels)

    def agent_timeout_s(self) -> float | None:
        """The task's own agent timeout for a live-container (TB2) trial, else None.

        Upstream Harbor bounds the agent phase by the task's `agent.timeout_sec`
        (`Trial._run_agent_phase` wraps the agent in `asyncio.wait_for`); on timeout it raises
        `AgentTimeoutError`, whose error policy still grades (`should_grade`, `has_valid_reward`),
        so the verifier runs on whatever the agent left and the reward stands.
        """
        if Capability.LIVE_CONTAINER not in self.task.meta.requires:
            return None
        value = self.task.data.get("agent_timeout_s")
        return float(value) if isinstance(value, (int, float)) and float(value) > 0 else None

    async def deliver(
        self,
        harness: Harness,
        index: int,
        text: str,
        new_session: bool,
        attached: tuple[str, ...],
        *,
        origin: RunOrigin = RunOrigin.MEMBER,
        approval: ApprovalDecision | None = None,
    ) -> bool:
        """One turn, waited on until the harness returns or `--turn-timeout` passes (none by default:
        the product under test owns its limits). False ends the trial (a non-completed turn)."""
        limits = [limit for limit in (self.s.turn_timeout_s, self.agent_timeout_s()) if limit is not None]
        effective_timeout = min(limits) if limits else None
        t_in = TurnInput(
            index=index,
            text=text,
            new_session=new_session,
            attachments=attached,
            origin=origin,
            approval=approval,
            deadline_s=effective_timeout,
        )
        self.t.delivered_turns.append(t_in)
        submitted = time.monotonic()
        out: TurnOutput | None = None
        deadline = asyncio.timeout(effective_timeout)
        try:
            async with deadline:
                out = await harness.turn(t_in)
        except Exception as exc:  # an adapter crash mid-turn is the harness's failure, not the eval's
            self.t.status, self.t.error = TrajectoryStatus.HARNESS_ERROR, f"turn {index}: {type(exc).__name__}: {exc}"
            if deadline.expired():  # status and error then come from the TIMEOUT turn, below
                error = f"turn {index}: still running after {effective_timeout or 0:.0f}s, killed"
                out = TurnOutput(status=TurnStatus.TIMEOUT, answer="", session_id=None, error=error)
        # Every turn's time counts toward agent_ms, a failed one's included.
        measured = int((time.monotonic() - submitted) * 1000)
        if out is None:
            # The adapter raised, perhaps while settling usage after the agent had stopped: count
            # only the part the harness says was the agent's.
            elapsed = harness.agent_elapsed_ms() if isinstance(harness, TurnTimed) else None
            self.t.turn_latency_ms.append(measured if elapsed is None else min(elapsed, measured))
            self.t.turn_split.append(None)
            return False
        self.t.turn_latency_ms.append(measured if out.latency_ms is None else min(out.latency_ms, measured))
        self.t.turn_split.append(out.split)
        self.t.flags |= out.flags
        self.t.answers[index - 1] = out.answer
        self.t.steps += out.steps
        self.t.approvals += out.approvals
        self.t.usage += out.usage
        observed_models = (*out.actual_models, *((out.actual_model,) if out.actual_model is not None else ()))
        for model in observed_models:
            if model not in self.t.actual_models:
                self.t.actual_models.append(model)
        if self.t.ttft_ms is None:
            self.t.ttft_ms = out.ttft_ms
        if out.status is not TurnStatus.COMPLETED:
            self.t.status, self.t.error = turn_status(out.status), out.error or f"turn {index}: {out.status.value}"
            return False
        return True

    # ---- evidence -----------------------------------------------------------------------

    async def snapshot(self, box: Box) -> Path | None:
        try:
            return await snapshot(box, self.dir)
        except (BoxError, OSError) as exc:
            self.t.notes.append(f"workspace snapshot failed: {exc}")
            return None

    async def snapshot_turn(self, box: Box, index: int) -> None:
        self.last_turn = index
        if index not in self.snapshot_turns:
            return
        try:
            self.turn_workspaces[index] = await snapshot(box, self.dir / "turns" / str(index))
        except (BoxError, OSError) as exc:
            self.t.notes.append(f"turn {index} workspace snapshot failed: {exc}")

    def turns_after(self, final: Path | None) -> dict[int, Path]:
        """Turn snapshots; a turn the trial never reached sees the final workspace, which nothing changed since."""
        out = dict(self.turn_workspaces)
        if final is not None:
            out |= {i: final for i in self.snapshot_turns if i > self.last_turn}
        return out

    async def world_evidence(self, control: ControlClient) -> WorldEvidence | None:
        try:
            world = await control.evidence()
        except (ControlError, httpx.HTTPError, ValueError) as exc:
            self.invalid.append(f"world evidence unavailable: {exc}")
            return None
        if world.internal_error is not None:
            self.invalid.append(f"world internal error: {world.internal_error}")
        return world

    async def meter_report(self) -> MeterReport | None:
        if self.meter_key is None:
            return None
        try:
            report = await self.s.svc.meter_client().report(self.meter_key)
        except (httpx.HTTPError, BoxError) as exc:
            self.invalid.append(f"meter unreachable: {exc}")
            return None
        return report

    async def revoke_meter(self) -> None:
        if self.meter_key is None:
            return
        key, self.meter_key = self.meter_key, None
        try:
            await self.s.svc.meter_client().revoke(key)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("meter credential revocation failed: %s", exc)

    async def verify(self, workspace: Path | None) -> dict[str, CommandOutcome]:
        commands = [c for c in self.task.checks.checks if isinstance(c, CommandCheck)]
        if not commands or workspace is None:
            return {}
        # The run's isolation, not the box's: a host-side control in a docker run still verifies
        # in parallax-verify, so its verdict is comparable with the boxed harnesses beside it.
        return await trusted_verify(self.task, workspace, commands, isolation=self.s.svc.isolation)

    # ---- grading and writing ------------------------------------------------------------

    async def graded(self, started: float, ev: Evidence | None, meter: MeterReport | None) -> TrialRun:
        checks: tuple[CheckResult, ...] = ()
        same_family = False
        execution_valid = None
        judged_under: str | None = None
        judged_score: float | None = None
        judge_cost: float | None = 0.0
        # A world-less task has no world evidence; its policy grades what the harness reported. An interrupted
        # trial measured no agent, so nothing is judged.
        gradable = ev is not None or self.task.seed is None
        if gradable and self.t.status is not TrajectoryStatus.BOOT_ERROR and not self.t.interrupted:
            g = await grade_checks(
                self.task,
                ev,
                self.s.panel,
                self.s.judge_config,
                self.plan.model,
                execution={
                    "status": self.t.status.value,
                    "error": self.t.error,
                    "notes": list(self.t.notes),
                    "tool_diagnostics": tool_diagnostics(self.t.steps),
                    **({"today": self.today} if self.today is not None else {}),
                },
                product=self.product,
                answers=self.t.answers,
                task_container=self.task_container,
                runner_recorded=self.tau2_recorded,
            )
            record_grade(self.dir / "judgments.jsonl", self.task, g)
            execution_valid = g.execution_valid
            if (
                execution_valid is None
                and self.task_container is not None
                and self.t.status is TrajectoryStatus.TIMEOUT
                and g.checks
                and all(c.passed is not None for c in g.checks)
            ):
                # Harbor's AgentTimeoutError policy still grades with a valid reward, so a
                # timed-out TB2 agent is scored by the verifier, not invalidated.
                execution_valid = True
            checks, same_family, judged_under, judged_score = g.checks, g.same_family, g.judge_identity, g.score
            costs = [j.cost() for j in g.judgments]
            unpriced = g.unpriced_calls + sum(n for _, n in costs)
            judge_cost = None if unpriced else sum(known for known, _ in costs)
        self.write_evidence(ev)
        finished = self.finish(started, meter, checks, same_family, execution_valid, judged_under, judged_score)
        return TrialRun(finished, ev, judge_cost)

    def finish(
        self,
        started: float,
        meter: MeterReport | None,
        checks: tuple[CheckResult, ...] = (),
        same_family: bool = False,
        execution_valid: bool | None = None,
        judge_identity: str | None = None,
        judged_score: float | None = None,
    ) -> TrialResult:
        wall = int((time.monotonic() - started) * 1000)
        usage = meter.usage if meter is not None else tuple(self.t.usage)
        # Metered: only what the provider's responses named ("meter first"); a harness's
        # own claim is often its configuration echoed back. Unmetered: the harness's parsed claim.
        metered = self.plan.harness.info.cost_source is CostSource.METERED
        if meter is not None:
            actual, unobserved = meter.actual_models, meter.unobserved_requests > 0
        else:
            actual, unobserved = tuple(dict.fromkeys(self.t.actual_models)), metered
        boot = [self.t.error] if self.t.status is TrajectoryStatus.BOOT_ERROR and self.t.error else []
        interrupted = [self.t.error] if self.t.interrupted and self.t.error else []
        direct_invalid = [*self.invalid, *boot, *interrupted]
        verdict, reason = decide(None, direct_invalid, self.t.status, checks, execution_valid=execution_valid)
        source = CostSource.METERED if meter is not None else self.plan.harness.info.cost_source
        panel = self.s.panel.models if self.s.panel is not None else ()
        row = result_row(
            self.plan, verdict=verdict, invalid_reason=reason, na_reason=None, status=self.t.status,
            checks=checks, usage=usage, cost_source=source, actual=actual, unobserved=unobserved,
            timings=(self.t.boot_ms, self.t.ttft_ms, wall, self.t.agent_ms), judge=panel, same_family=same_family,
            web=trial_web_isolation(self.plan.harness.info, self.box_iso), rel_dir=self.plan.rel_dir,
            harness_flags=frozenset(self.t.flags), split=self.t.latency_split, metrics=self.t.metrics,
            reported=self.t, judge_identity=judge_identity, judged_score=judged_score,
        )  # fmt: skip
        self.write_trajectory(row, usage, actual)
        self.write(self.dir / "result.json", row.model_dump_json(indent=2))
        return row

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(redact(text, secret_values(self.secrets)), encoding="utf-8")

    def write_trajectory(self, row: TrialResult, usage: tuple[Usage, ...], actual: tuple[str, ...]) -> None:
        traj = Trajectory(
            status=self.t.status, error=self.t.error, answers=tuple(self.t.answers), steps=tuple(self.t.steps),
            delivered_turns=tuple(self.t.delivered_turns), execution_invalid_reasons=tuple(self.invalid),
            usage=usage, actual_models=actual, harness_version=row.harness_version, boot_ms=self.t.boot_ms,
            ttft_ms=self.t.ttft_ms, wall_ms=row.wall_ms or 0, agent_ms=self.t.agent_ms,
            turn_latency_ms=tuple(self.t.turn_latency_ms), turn_split=tuple(self.t.turn_split),
            approvals=tuple(self.t.approvals), extra={**self.t.extra, "notes": list(self.t.notes)},
        )  # fmt: skip
        self.write(self.dir / "trajectory.json", traj.model_dump_json(indent=2))
        log = [f"status: {self.t.status.value}", f"error: {self.t.error}", *self.t.notes]
        if self.raw_output:
            log.append(
                f"--- raw harness output (newest {RAW_OUTPUT_CHARS // (1024 * 1024)} MiB) ---\n{self.raw_output}"
            )
        self.write(self.dir / "harness.log", "\n".join(log) + "\n")

    def write_evidence(self, ev: Evidence | None) -> None:
        if ev is None:
            return
        world = ev.world
        self.write(self.dir / "world_evidence.json", world.model_dump_json(indent=2))
        self.write(self.dir / "world_log.jsonl", "".join(c.model_dump_json() + "\n" for c in world.calls))
        self.write(self.dir / "world_final.json", json.dumps(world.final, indent=2, ensure_ascii=False))
        self.write(self.dir / "world_diff.json", world.diff.model_dump_json(indent=2))
        for check_id, outcome in ev.verify.items():
            self.write(self.dir / "verify" / f"{safe_segment(check_id)}.log", outcome.model_dump_json(indent=2))


# ---------------------------------------------------------------------------
# planning, resume and the run
# ---------------------------------------------------------------------------


def trial_name(plan: TrialPlan) -> str:
    """The trial's id as its harness and the budget journal see it; every attempt of a trial shares it."""
    return f"{plan.task.id}.{plan.harness.name}.{safe_segment(plan.model)}.{plan.repeat}"


def harness_config_hash(harness: Harness) -> str:
    return harness.config_hash if isinstance(harness, Configured) else config_hash(harness.info)


def probe_tasks(tasks: Sequence[Task], info: HarnessInfo, isolation: Isolation) -> dict[BoxKind, Task]:
    """One task per kind of box the trials will run in: a remote harness with a device box runs
    some trials in the host's install and others in its image, and each must be probed."""
    by_kind: dict[BoxKind, Task] = {}
    for task in tasks:
        by_kind.setdefault(box_kind(task.meta.requires, info, isolation), task)
    return by_kind


async def plan_harness(name: str, tasks: Sequence[Task], run_isolation: Isolation, network: str | None) -> HarnessPlan:
    """Load a harness and learn its version in a throwaway box per box kind the tasks need. A kind
    whose probe fails makes only its own trials INVALID; every kind failing, or two kinds reporting
    different versions, makes all of them INVALID, not a crash."""
    harness = load_harness(name)
    info, cfg = harness.info, harness_config_hash(harness)
    if (reason := credentials_unusable(info, os.environ)) is not None:
        return HarnessPlan(name=name, info=info, version=None, config_hash=cfg, unavailable=reason)
    isolation = harness_isolation(info, run_isolation)
    versions: set[str] = set()
    failed: dict[BoxKind, str] = {}
    for kind, task in probe_tasks(tasks, info, isolation).items():
        try:
            box = await open_box(task, info, isolation, network)
            try:
                versions.add(await harness.version(box))
            finally:
                await box.close()
        except (HarnessError, BoxError, OSError) as exc:
            failed[kind] = str(exc)
    if not versions:
        reason = "; ".join(f"{kind} box: {why}" for kind, why in failed.items())
        return HarnessPlan(name=name, info=info, version=None, config_hash=cfg, unavailable=reason)
    if len(versions) != 1:
        found = ", ".join(sorted(versions))
        reason = f"host install and device image report different versions ({found}); install one version"
        return HarnessPlan(name=name, info=info, version=None, config_hash=cfg, unavailable=reason)
    (version,) = versions
    return HarnessPlan(name=name, info=info, version=version, config_hash=cfg, unavailable_boxes=failed)


def models_for(info: HarnessInfo, models: Sequence[str | None]) -> tuple[str | None, ...]:
    match info.kind:
        case HarnessKind.CONTROL:
            # A model-driven control (a plugin's baseline agent) still needs a model to drive it.
            if not info.pins_model:
                return (None,)
        case HarnessKind.OPEN | HarnessKind.PRODUCT:
            pass
        case _:
            assert_never(info.kind)
    chosen = tuple(m for m in models if m is not None)
    if chosen:
        return chosen
    if info.pins_model:
        raise ValueError(f"harness {info.name} pins a model: pass --model")
    return (None,)


def plan_trials(
    tasks: Sequence[Task], harnesses: Sequence[HarnessPlan], models: Sequence[str | None], opts: RunOptions
) -> list[TrialPlan]:
    plans: list[TrialPlan] = []
    policy = policy_identity(tasks)
    for hp in harnesses:
        for model in models_for(hp.info, models):
            for task in tasks:
                for repeat in range(opts.repeats):
                    key = fingerprint(
                        task.hash, hp.name, hp.version or "unavailable", hp.config_hash, model, repeat, opts.judge,
                        f"{__version__}:{policy}", isolation=opts.isolation, capabilities=hp.info.capabilities,
                    )  # fmt: skip
                    plans.append(TrialPlan(task=task, harness=hp, model=model, repeat=repeat, key=key))
    return plans


def only_pairs(plans: Sequence[TrialPlan], pairs: Sequence[tuple[str, str]]) -> list[TrialPlan]:
    """The plans of the named (task id, model) pairs, in planned order; every plan when no pair is named. A pair
    that matches no plan is an error, so a typo never shrinks a run in silence."""
    if not pairs:
        return list(plans)
    wanted = set(pairs)
    missing = wanted - {(p.task.id, p.model) for p in plans}
    if missing:
        raise ValueError(f"--pair matches no planned trial: {', '.join(f'{t}={m}' for t, m in sorted(missing))}")
    return [p for p in plans if (p.task.id, p.model) in wanted]


def read_results(path: Path) -> list[TrialResult]:
    if not path.is_file():
        return []
    return [TrialResult.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def latest_results(path: Path) -> list[TrialResult]:
    """One row per resume key, the last winning: a retried or resumed trial supersedes its own attempt.

    `results.jsonl` is append-only, so a transient-invalid trial that was re-run has two rows under one
    key. Everything that counts or grades trials reads them through this, so the earlier attempt is kept
    as evidence without being scored twice.
    """
    by_key: dict[str, TrialResult] = {}
    for row in read_results(path):
        by_key[row.key] = row
    return list(by_key.values())


def attempts_recorded(path: Path) -> dict[str, int]:
    """Per resume key, the attempt number its next attempt takes: one past the highest recorded."""
    found: dict[str, int] = {}
    for row in read_results(path):
        found[row.key] = max(found.get(row.key, 0), row.attempt + 1)
    return found


def transient_invalid(result: TrialResult) -> bool:
    """Whether an INVALID trial is an execution fault another attempt can plausibly avoid.

    Every reason the row carries must be one the runner itself recorded for a fault of that attempt. A
    reason outside `RETRYABLE_INVALID` keeps the trial as it stands: the run could not open that harness
    or box, the trial's own world state lacked the evidence a check needed, or the semantic judge
    rejected the execution it produced. A failed teardown is never retried, because a second attempt
    would strand more of the harness's remote resources rather than fewer.
    """
    if result.verdict is not Verdict.INVALID or not result.invalid_reason:
        return False
    # `decide` joins every reason it found with "; ", and appends this qualifier to a short-execution row.
    reasons = [
        r.strip()
        for r in result.invalid_reason.split(";")
        if r.strip() and r.strip() != "attribution requires semantic judging"
    ]
    if any(never in reason for reason in reasons for never in NEVER_RETRYABLE_INVALID):
        return False
    return bool(reasons) and all(reason.startswith(RETRYABLE_INVALID) for reason in reasons)


def completed_keys(run_dir: Path, plans: Sequence[TrialPlan]) -> set[str]:
    """Keys to skip on resume. A planned trial whose stored key differs means an input changed (task,
    harness version, config, judge, parallax, isolation): the old file is rotated, never mixed in."""
    path = run_dir / RESULTS
    rows = read_results(path)
    planned = {p.identity: p.key for p in plans}
    stale = [r for r in rows if planned.get((r.task_id, r.harness, r.model, r.repeat), r.key) != r.key]
    if stale:
        print(
            f"warning: {len(stale)} stored result(s) no longer match their fingerprint "
            f"(e.g. {stale[0].task_id}/{stale[0].harness}); moving {RESULTS} to {RESULTS}.stale",
            file=sys.stderr,
        )
        path.replace(path.with_name(f"{RESULTS}.stale"))
        return set()
    return {r.key for r in rows if r.verdict is not Verdict.INVALID}


def default_out() -> Path:
    return Path("runs") / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def judge_setup(models: Sequence[str]) -> tuple[JudgePanel | None, JudgeConfig | None]:
    panel = JudgePanel(models=tuple(models)) if models else panel_from_env()
    if panel is None:
        return None, None
    return panel, config_from_env()


def model_egress_hosts(environ: Mapping[str, str]) -> tuple[str, ...]:
    hosts = {
        host
        for wire, default in DEFAULT_UPSTREAM.items()
        if (host := urlsplit(environ.get(f"PARALLAX_UPSTREAM_{wire.upper()}") or default).hostname) is not None
    }
    return tuple(sorted(hosts))


def live_session(out: Path) -> str:
    """The run directory's live feed session: the one its manifest already holds on a resume, else a new one. The
    directory name stays readable in it; the suffix keeps runs whose directories share a name apart."""
    manifest = out / "manifest.json"
    if manifest.is_file() and (kept := RunManifest.model_validate_json(manifest.read_text()).live_session):
        return kept
    return f"{out.name}-{secrets.token_hex(4)}"


def manifest_for(
    opts: RunOptions, plans: Sequence[TrialPlan], harnesses: Sequence[HarnessPlan], session: str, run_id: str
) -> RunManifest:
    tasks = {p.task.id: p.task.hash for p in plans}
    return RunManifest(
        run_id=run_id, live_session=session, created_at=datetime.now(UTC), parallax_version=__version__,
        harnesses=tuple(h.info for h in harnesses),
        models=tuple(dict.fromkeys(p.model for p in plans)), tasks=tuple(sorted(tasks.items())),
        repeats=opts.repeats, judge=opts.judge, isolation=opts.isolation, selection=opts.selection,
        grading_policy=policy_identity(list({p.task.id: p.task for p in plans}.values())),
    )  # fmt: skip


async def run_async(
    tasks: Sequence[Task], harnesses: Sequence[str], models: Sequence[str | None], opts: RunOptions
) -> Path:
    chosen = [t for t in tasks if opts.include_xfail or t.meta.xfail is None]
    extensions.check_lanes(chosen, harnesses, opts.concurrency)
    if not chosen:
        raise ValueError("no tasks to run (xfail tasks need --include-xfail)")
    if (opts.budget_usd is None) != (opts.reserve_usd is None):
        raise ValueError("a campaign budget needs both its limit (--budget) and each trial's reservation (--reserve)")
    if opts.budget_journal is not None and opts.budget_usd is None and not opts.budget_journal.is_file():
        # A named journal is a budget to reopen; without limits to create it, a missing one must not mean no budget.
        raise ValueError(
            f"no campaign budget journal at {opts.budget_journal}; pass --budget and --reserve to start one"
        )
    panel, judge_config = judge_setup(opts.judge)
    opts = opts.model_copy(update={"judge": panel.models if panel else ()})
    infos = [load_harness(h).info for h in harnesses]
    need_meter = any(i.cost_source is CostSource.METERED for i in infos)
    egress_hosts = (*model_egress_hosts(os.environ), *(host for info in infos for host in info.egress_hosts))
    async with services(
        opts.isolation, opts.world_public_url, need_meter, opts.out.name, opts.world_port, egress_hosts
    ) as svc:
        plans_h = [await plan_harness(h, chosen, opts.isolation, svc.network) for h in harnesses]
        plans = only_pairs(plan_trials(chosen, plans_h, models, opts), opts.pairs)
        opts.out.mkdir(parents=True, exist_ok=True)  # only now: a run interrupted in setup leaves no husk
        # A fresh uuid per run (never the output basename, which two runs can share): it labels
        # every task container this run starts, is recorded in manifest.json, and cleanup sweeps
        # only containers carrying this uuid, so a same-named run's containers are untouched.
        run_uuid = uuid.uuid4().hex
        feed_session = live_session(opts.out)
        (opts.out / "manifest.json").write_text(
            manifest_for(opts, plans, plans_h, feed_session, run_uuid).model_dump_json(indent=2)
        )
        prov = provenance.collect(opts.out, opts.provenance_extra)
        provenance.write(opts.out, prov)
        done = completed_keys(opts.out, plans)
        recorded = attempts_recorded(opts.out / RESULTS)
        todo = start_order([replace(p, attempt=recorded.get(p.key, 0)) for p in plans if p.key not in done])
        budget = None
        journal = opts.budget_journal or opts.out / BUDGET_JOURNAL
        if opts.budget_usd is not None and opts.reserve_usd is not None:
            budget = CampaignBudget(opts.budget_usd, opts.reserve_usd, journal)
        elif journal.is_file():
            budget = CampaignBudget.from_journal(journal)
        session = Session(svc, opts.out, panel, judge_config, opts.turn_timeout_s, budget, opts.videos, run_id=run_uuid)
        session.live = LiveFeed.open(feed_session)
        if session.live is not None:
            session.live.run_started(
                total=len(plans), already_done=len(plans) - len(todo), concurrency=max(1, opts.concurrency),
                labels=sorted({live_label(p.harness.name, p.model) for p in plans}), kept=sorted({p.key for p in plans} - {p.key for p in todo}),
                planned=[p.key for p in todo],
            )  # fmt: skip
        from parallax.harbor_tasks import sweep_run_containers

        await sweep_run_containers(session.run_id)  # containers a crashed earlier run left behind
        try:
            runs = {r.result.key: r for r in await execute_all(session, todo, opts, retries_left=opts.retry_invalid)}
            await retry_transient_invalid(session, todo, runs, opts)
        finally:
            await sweep_run_containers(session.run_id)
            if session.live is not None:
                session.live.run_finished()
            if budget is not None:
                budget.close()
    served = provenance.served_models(read_results(opts.out / RESULTS))
    provenance.write(opts.out, prov.model_copy(update={"served_models": served}))
    return opts.out


def expected_turns(task: Task) -> int:
    """The run's duration prior for a task: each step and each scheduled fire is one agent turn."""
    return len(task.turns) + len(task.meta.fires)


def start_order(plans: Sequence[TrialPlan]) -> list[TrialPlan]:
    """The order `execute_all` admits trials: most expected turns first, so the slowest trials do
    not start last and set the run's tail. The sort is stable, so equal tasks keep their plan order,
    and each task's trials for every harness, model and repeat start side by side."""
    first: dict[str, int] = {}
    for i, plan in enumerate(plans):
        first.setdefault(plan.task.id, i)
    return sorted(plans, key=lambda p: (-expected_turns(p.task), first[p.task.id]))


async def retry_transient_invalid(
    session: Session, plans: Sequence[TrialPlan], runs: dict[str, TrialRun], opts: RunOptions
) -> dict[str, TrialRun]:
    """Re-run every trial that ended INVALID for a transient execution fault, at most `retry_invalid` times.

    This is the same mechanism as `--resume` (an invalid key is not a completed trial), applied inside one
    run: the re-run appends its row under the same key with the next `attempt` and a directory of its own, so
    the earlier attempt stays in `results.jsonl` and on disk as evidence and `latest_results` scores one trial.
    A trial whose reason is outside `RETRYABLE_INVALID` is never attempted again, and a PASS or FAIL is never
    overwritten: only an INVALID row is re-run. Each round first waits `retry_wait_s`, doubled per round, so an
    outage (`ENVIRONMENT_INTERRUPTION`) has time to clear.
    """
    for round_ in range(opts.retry_invalid):
        again = [
            replace(p, attempt=runs[p.key].result.attempt + 1)
            for p in plans
            if p.key in runs and transient_invalid(runs[p.key].result)
        ]
        if not again:
            break
        wait = opts.retry_wait_s * 2**round_
        print(f"retrying {len(again)} transient-invalid trial(s) (round {round_ + 1}) in {wait:g}s", file=sys.stderr)
        await asyncio.sleep(wait)
        retried = await execute_all(session, again, opts, retries_left=opts.retry_invalid - round_ - 1)
        runs = {**runs, **{r.result.key: r for r in retried}}
    return runs


async def execute_all(
    session: Session, todo: Sequence[TrialPlan], opts: RunOptions, *, retries_left: int = 0
) -> list[TrialRun]:
    """Run `todo`; `retries_left` is how many more rounds `retry_transient_invalid` will run after this one."""
    gate = asyncio.Semaphore(max(1, opts.concurrency))
    results_path = opts.out / RESULTS
    finished: list[TrialRun] = []

    async def one(plan: TrialPlan) -> TrialRun:
        async with gate:
            if session.live is not None:
                session.live.trial_started(
                    key=plan.key, task_id=plan.task.id, label=live_label(plan.harness.name, plan.model),
                    repeat=plan.repeat, attempt=plan.attempt,
                )  # fmt: skip
            run = await session.trial(plan)
        row = redact(run.result.model_dump_json(), secret_values())
        with results_path.open("a", encoding="utf-8") as fh:
            fh.write(row + "\n")
        if session.live is not None:
            # The row as written, so the feed crosses the same secret-scrubbing boundary as results.jsonl.
            sent = TrialResult.model_validate_json(row)
            # A transient INVALID with attempts left is run again after the retry wait, so it is not done yet.
            pending = transient_invalid(sent) and retries_left > 0
            session.live.trial_finished(sent, live_label(sent.harness, sent.model), retry_pending=pending)
        finished.append(run)
        if not opts.quiet:
            r = run.result
            reason = r.invalid_reason or r.na_reason or ""
            print(f"[{len(finished)}/{len(todo)}] {r.task_id} {r.harness} {r.model or '-'} #{r.repeat}: "
                  f"{r.verdict.value} {reason}".rstrip(), file=sys.stderr)  # fmt: skip
        return run

    return list(await asyncio.gather(*(one(p) for p in todo)))


def run(tasks: Sequence[Task], harnesses: Sequence[str], models: Sequence[str | None], opts: RunOptions) -> Path:
    return run_to_teardown(run_async(tasks, harnesses, models, opts))


def invalid_share(results: Sequence[TrialResult]) -> float:
    ran = [r for r in results if r.verdict is not Verdict.NA]
    return sum(r.verdict is Verdict.INVALID for r in ran) / len(ran) if ran else 0.0
