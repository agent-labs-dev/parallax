"""Where a harness runs, and the sidecar that serves it.

- `HostBox`: a temp dir on the host (host and remote isolation; dev only for web isolation).
- `DockerBox`: one long-lived container per trial on the internal `px-<run>` network, so the
  only thing it can reach is the sidecar (world + meter). That is what makes
  `web_isolation=enforced` true rather than asserted.
- A device box (`DockerBox.create_device`): a container on docker's default bridge, with egress, for
  a remote product whose own device daemon must reach the product's API (`RemoteBox.DEVICE`). It
  enforces nothing about the web; a remote harness's web isolation is `none` or `flags` anyway.
- `Sidecar`: the per-run world+meter container, on `px-<run>` (alias `sidecar`) and on the
  default bridge, the one container with egress (the meter's upstream calls leave from here).

Docker is driven through its CLI (`asyncio.create_subprocess_exec("docker", ...)`): no SDK, and
the argv is always a list, never a shell string.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import logging
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import distributions
from pathlib import Path
from typing import assert_never

import httpx

from parallax import __version__
from parallax.desktop import EXEC as DESKTOP_EXEC
from parallax.meter import ADMIN_TOKEN_ENV as METER_ADMIN_ENV
from parallax.models import (
    Capability,
    CommandCheck,
    CommandOutcome,
    HarnessInfo,
    Isolation,
    Model,
    RemoteBox,
    Task,
    WebIsolation,
)

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
VERIFY_IMAGE = "parallax-verify:py3.13"
WORLD_PORT = 7801
METER_PORT = 7802
WORLD_ADMIN_ENV = "PARALLAX_WORLD_ADMIN_TOKEN"
BOX_WORKSPACE = Path("/workspace")
BOX_HOME = Path("/home/agent")
# The box's wall clock is the world's: every harness process runs under `px-clock`
# (px_clock.c), which shifts wall-clock reads by the offset in this root-owned file. Harnesses
# put the date they read from the system into their own context, so a box on real time would
# contradict the scenario. Host isolation keeps real time: it is for controls and dev only.
CLOCK_BIN = "/usr/local/bin/px-clock"
CLOCK_FILE = Path("/etc/parallax/clock")
# Host env a harness process may inherit in host isolation; everything else (notably provider
# keys) stays out, so a harness can only spend through the meter.
DOCKER_CLIENT_ENV = frozenset({"PATH", "HOME"})
HOST_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TERM", "USER", "SHELL", "TMPDIR")
STDERR_TAIL_BYTES = 8192
# A pipe still open after a one-shot process exits is held by a descendant it left behind. Reading
# stops PIPE_LINGER_S after the exit; a caller whose output is complete at the exit (and fails loudly
# when it is not) may stop once the pipes have been quiet for PIPE_QUIET_S instead.
PIPE_QUIET_S = 0.2
PIPE_LINGER_S = 5.0
# Older supported Python 3.13 releases can delay Process.wait() until inherited pipes close.
# Poll the child watcher's exit status at this interval (see `wait_exit`).
EXIT_POLL_S = 0.02
STREAM_LINE_LIMIT = 16 * 1024 * 1024  # ACP / NDJSON lines carry whole tool schemas
BOX_CPUS = "2"
BOX_MEMORY = "4096m"
READY_TIMEOUT_S = 60.0
# Device boxes join docker's default bridge: the product's daemon inside must reach its API.
DEVICE_NETWORK = "bridge"
DEVICE_LABEL = "parallax.device-box"
DEVICE_OWNER_HOST = "parallax.device-owner-host"
DEVICE_OWNER_PID = "parallax.device-owner-pid"
DEVICE_OWNER_STARTED = "parallax.device-owner-started"
# Meter configuration the sidecar inherits by name from the runner's environment.
UPSTREAM_ENV = (
    "PARALLAX_UPSTREAM_OPENAI",
    "PARALLAX_UPSTREAM_OPENAI_KEY",
    "PARALLAX_UPSTREAM_ANTHROPIC",
    "PARALLAX_UPSTREAM_ANTHROPIC_KEY",
)


class BoxError(RuntimeError):
    """Infrastructure failure (docker, image build, sidecar boot): the runner's exit code 3 / boot_error."""


class SidecarUrls(Model):
    world_internal: str  # as boxes see it: http://sidecar:7801/mcp
    world_host: str  # as the runner sees it: http://127.0.0.1:<port>/mcp
    meter_internal: str  # http://sidecar:7802
    meter_host: str  # http://127.0.0.1:<port>


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Completed:
    code: int
    out: str
    err: str


async def run(argv: Sequence[str], *, stdin: bytes | None = None, timeout_s: float = 600.0) -> Completed:
    """Run a short control command (docker CLI, uv build) to completion."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout_s)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise BoxError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} timed out after {timeout_s:.0f}s") from exc
    if proc.returncode is None:
        raise BoxError(f"{argv[0]} exited without a status")
    return Completed(proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"))


async def docker(*args: str, stdin: bytes | None = None, timeout_s: float = 600.0) -> str:
    """`docker <args>`; a non-zero exit is a BoxError carrying docker's stderr."""
    try:
        done = await run(("docker", *args), stdin=stdin, timeout_s=timeout_s)
    except FileNotFoundError as exc:
        raise BoxError("docker CLI not found on PATH") from exc
    if done.code != 0:
        raise BoxError(f"docker {' '.join(args[:2])} failed ({done.code}): {done.err.strip()[-2000:]}")
    return done.out


async def process_start_stamp(pid: int) -> str | None:
    """A process identity that survives PID reuse; None only when ps reports it absent."""
    result = await run(("env", "LC_ALL=C", "TZ=UTC", "ps", "-p", str(pid), "-o", "lstart="), timeout_s=10)
    if result.code == 1 and not result.out.strip():
        return None
    if result.code != 0 or not result.out.strip():
        raise BoxError(f"could not read device owner process {pid}: {result.err}")
    return hashlib.sha256(result.out.strip().encode()).hexdigest()


async def sweep_orphan_device_boxes() -> None:
    """Remove this host's boxes whose owning runner exited, regardless of trial age."""
    rows = await docker(
        "ps",
        "-a",
        "--filter",
        f"label={DEVICE_LABEL}",
        "--filter",
        f"label={DEVICE_OWNER_HOST}={socket.gethostname()}",
        "--format",
        '{{.ID}} {{.Label "' + DEVICE_OWNER_PID + '"}} {{.Label "' + DEVICE_OWNER_STARTED + '"}}',
    )
    for row in rows.splitlines():
        container, owner_pid, started = row.split()
        if await process_start_stamp(int(owner_pid)) != started:
            logger.warning("removing orphan device box %s (runner %s exited)", container, owner_pid)
            await docker("rm", "-f", container)


class TailBuffer:
    def __init__(self, limit: int = STDERR_TAIL_BYTES) -> None:
        self.chunks: deque[bytes] = deque()
        self.size = 0
        self.limit = limit

    def add(self, chunk: bytes) -> None:
        self.chunks.append(chunk)
        self.size += len(chunk)
        while self.size - len(self.chunks[0]) >= self.limit:
            self.size -= len(self.chunks.popleft())

    def text(self) -> str:
        return b"".join(self.chunks)[-self.limit :].decode(errors="replace")


async def drain(stream: asyncio.StreamReader | None, sink: bytearray | TailBuffer) -> None:
    if stream is None:
        return
    while chunk := await stream.read(65536):
        if isinstance(sink, TailBuffer):
            sink.add(chunk)
        else:
            sink.extend(chunk)


def observe_drains(readers: asyncio.Future[tuple[None, None]]) -> None:
    """Retrieve the drains' outcome when nobody awaits it: a caller cancelled mid-command (the run
    shutting down) leaves the gather behind, and a drain the loop then cancels is recorded on it as
    a CancelledError exception that would otherwise be logged as never retrieved. A drain failure
    that is awaited reaches `communicate`'s caller as well; this only keeps an abandoned one visible."""
    if readers.cancelled():
        return
    exc = readers.exception()
    if exc is not None and not isinstance(exc, asyncio.CancelledError):
        logger.debug("process output drain failed: %r", exc)


async def wait_exit(proc: asyncio.subprocess.Process, timeout_s: float | None) -> int | None:
    """The process's own exit status, or None if it is still running after `timeout_s` (None: wait).

    On older supported Python 3.13 patch releases, waiters may wake only after every stdio pipe
    disconnects. A descendant holding a pipe can outlive the actual process. Polling the child
    watcher's returncode works on those releases as well as versions containing gh-119710.
    """
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    while proc.returncode is None:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        await asyncio.sleep(EXIT_POLL_S)
    return proc.returncode


async def settle_pipes(
    readers: asyncio.Future[tuple[None, None]], out: bytearray, err: bytearray, exited_at: float, quiet_s: float | None
) -> None:
    """Finish reading an exited process's pipes, giving up on a descendant that holds them open after
    PIPE_LINGER_S, or after `quiet_s` without new output. The quiet cutoff is opt-in: a descendant may
    still be writing output that matters (a grading check's, a wrapper's worker), and cutting it off
    would truncate it silently."""
    seen = -1
    while not readers.done():
        size = len(out) + len(err)
        if (quiet_s is not None and size == seen) or time.monotonic() - exited_at >= PIPE_LINGER_S:
            readers.cancel()
            logger.info("process pipes still open %.1fs after it exited; a descendant holds them",
                        time.monotonic() - exited_at)  # fmt: skip
            return
        seen = size
        await asyncio.wait((readers,), timeout=quiet_s if quiet_s is not None else PIPE_LINGER_S)
    await readers  # done: a drain's own failure reaches the caller


class Killer:
    """How to kill a process tree; differs because killing a `docker exec` client leaves the
    in-container process running."""

    def __init__(self, proc: asyncio.subprocess.Process, box: DockerBox | None, pid_file: str | None) -> None:
        self.proc = proc
        self.box = box
        self.pid_file = pid_file

    async def kill(self) -> None:
        if self.box is not None and self.pid_file is not None:
            with contextlib.suppress(BoxError):
                await docker(
                    "exec",
                    "-u",
                    self.box.user,
                    self.box.container,
                    "sh",
                    "-c",
                    # `-s KILL --` is the one group-kill spelling dash, bash and busybox all accept.
                    'test -f "$1" && kill -s KILL -- -"$(cat "$1")" 2>/dev/null; rm -f "$1"; true',
                    "sh",
                    self.pid_file,
                    timeout_s=15,
                )
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                if self.box is None:
                    os.killpg(self.proc.pid, signal.SIGKILL)  # start_new_session made pid the pgid
                else:
                    self.proc.kill()
            # Not `self.proc.wait()`: a descendant that survived the kill (its own session, so outside
            # the group) would hold the pipes open and keep it pending forever. The process is reaped
            # regardless, so wait on `returncode` instead.
            await wait_exit(self.proc, None)


class ExecProc:
    """`Proc`: one-shot process; stdout/stderr drained concurrently so a full pipe never deadlocks."""

    def __init__(self, proc: asyncio.subprocess.Process, killer: Killer) -> None:
        self.proc = proc
        self.killer = killer
        self.exited_at: float | None = None

    async def communicate(
        self, stdin: bytes | None, timeout_s: float | None, *, quiet_s: float | None = None
    ) -> tuple[int | None, str, str]:
        """Returns `(None, out, err)` on timeout, after killing the process tree. `timeout_s` None
        waits for the process to exit however long it takes, or until the wait is cancelled, which kills
        the tree (an agent's turn: `--turn-timeout`). `quiet_s`: see `settle_pipes`."""
        out, err = bytearray(), bytearray()
        readers = asyncio.gather(drain(self.proc.stdout, out), drain(self.proc.stderr, err))
        readers.add_done_callback(observe_drains)
        if self.proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                if stdin:
                    self.proc.stdin.write(stdin)
                    await self.proc.stdin.drain()
                self.proc.stdin.close()
        try:
            code = await wait_exit(self.proc, timeout_s)
        except asyncio.CancelledError:  # the turn deadline or an interrupt: take the tree with it
            await self.killer.kill()
            raise
        if code is None:
            await self.killer.kill()
        self.exited_at = time.monotonic()
        await settle_pipes(readers, out, err, self.exited_at, quiet_s)
        return code, out.decode(errors="replace"), err.decode(errors="replace")

    async def kill(self) -> None:
        await self.killer.kill()


# ---------------------------------------------------------------------------
# boxes
# ---------------------------------------------------------------------------


def web_isolation_for(isolation: Isolation) -> WebIsolation:
    match isolation:
        case Isolation.DOCKER:
            return WebIsolation.ENFORCED
        case Isolation.HOST:
            return WebIsolation.FLAGS
        case Isolation.REMOTE:
            return WebIsolation.NONE
        case _:
            assert_never(isolation)


def seed_dir(task: Task) -> Path:
    return task.path / "environment" / "files"


class HostBox:
    """`BoxHandle` on the host: workspace and HOME are dirs under one temp root."""

    def __init__(self, root: Path, isolation: Isolation) -> None:
        self.root = root
        self.isolation = isolation
        self.web_isolation = web_isolation_for(isolation)
        self.workspace = root / "workspace"
        self.home = root / "home"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)

    @classmethod
    def create(cls, task: Task, isolation: Isolation) -> HostBox:
        box = cls(Path(tempfile.mkdtemp(prefix=f"px-{task.id}-")), isolation)
        if seed_dir(task).is_dir():
            shutil.copytree(seed_dir(task), box.workspace, dirs_exist_ok=True)
        return box

    def env(self, extra: Mapping[str, str]) -> dict[str, str]:
        base = {k: os.environ[k] for k in HOST_ENV_ALLOWLIST if k in os.environ}
        return {**base, "HOME": str(self.home), **extra}

    async def exec(self, argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(env),
            cwd=cwd,
            start_new_session=True,  # own process group, so kill() takes the whole tree
            limit=STREAM_LINE_LIMIT,
        )

    async def spawn(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> ExecProc:
        proc = await self.exec(argv, env, cwd)
        return ExecProc(proc, Killer(proc, None, None))

    async def set_clock(self, now: datetime) -> None:
        """Host boxes run on the host's real clock; host isolation is for controls and dev."""

    async def write_file(self, path: Path, data: bytes) -> None:
        target = path.resolve()
        if not (target.is_relative_to(self.workspace.resolve()) or target.is_relative_to(self.home.resolve())):
            raise ValueError(f"{path} is outside the box's workspace and home")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class DockerBox:
    """`BoxHandle` in a container; `workspace`/`home` are container paths."""

    def __init__(
        self,
        container: str,
        user: str = "agent",
        *,
        clock: bool = False,
        isolation: Isolation = Isolation.DOCKER,
        web_isolation: WebIsolation = WebIsolation.ENFORCED,
        desktop: bool = False,
    ) -> None:
        self.container = container
        self.user = user  # images built from box.Dockerfile run harnesses as `agent`
        self.clock = clock  # run every process under px-clock (only images built by box.py have it)
        self.isolation = isolation
        self.web_isolation = web_isolation
        self.desktop = desktop
        self.workspace = BOX_WORKSPACE
        self.home = BOX_HOME

    @classmethod
    async def create(cls, task: Task, image: str, net: str) -> DockerBox:
        name = f"{net}-box-{secrets.token_hex(4)}"
        run_args = ("--label", f"parallax.net={net}", "--network", net)
        return await cls.boot(task, image, cls(name, clock=True), run_args, ("sleep", "infinity"))

    @classmethod
    async def create_device(cls, task: Task, image: str) -> DockerBox:
        """A box for a remote harness's device daemon (`RemoteBox.DEVICE`): egress, so the daemon
        reaches the product's API, and the hostname is the container's name, so the device the
        daemon registers can be told apart from every other device in the account.

        Not on the world's clock: the daemon and the CLI talk TLS to the product, and a certificate
        issued after the world's date would read as not yet valid. The product is told the world's
        clock over its own API instead."""
        await sweep_orphan_device_boxes()
        started = await process_start_stamp(os.getpid())
        if started is None:
            raise BoxError("the device owner process disappeared during setup")
        name = f"px-device-{secrets.token_hex(4)}"
        run_args = (
            "--rm",
            # The daemon is a child of the long-lived container command. Docker's init
            # reaps it after shutdown, so the CLI's stop wait cannot mistake a zombie
            # for a live daemon.
            "--init",
            "--label",
            DEVICE_LABEL,
            "--label",
            f"{DEVICE_OWNER_HOST}={socket.gethostname()}",
            "--label",
            f"{DEVICE_OWNER_PID}={os.getpid()}",
            "--label",
            f"{DEVICE_OWNER_STARTED}={started}",
            "--network",
            DEVICE_NETWORK,
            "--hostname",
            name,
        )
        box = cls(
            name, isolation=Isolation.REMOTE, web_isolation=WebIsolation.NONE, desktop=task.meta.desktop is not None
        )
        try:
            await cls.boot(task, image, box, run_args, ("sleep", "infinity"))
            if task.meta.desktop is not None:
                await box.start_desktop(task)
            return box
        except BaseException:
            await box.close()
            raise

    async def start_desktop(self, task: Task) -> None:
        if task.meta.desktop is None:
            raise BoxError("desktop task has no app specification")
        await docker(
            "exec",
            "-d",
            "-u",
            self.user,
            self.container,
            "sh",
            "-c",
            'exec dbus-run-session -- python -m parallax.desktop serve "$1" > /tmp/px-desktop.log 2>&1',
            "sh",
            task.meta.desktop.model_dump_json(),
        )
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            state = (await docker("exec", self.container, "python", "-m", "parallax.desktop", "status")).strip()
            if state == "ready":
                return
            if state != "starting" or time.monotonic() >= deadline:
                log = await docker("exec", self.container, "cat", "/tmp/px-desktop.log")
                raise BoxError(f"desktop startup gate: {state}; {log[-2000:]}")
            await asyncio.sleep(0.2)

    @staticmethod
    async def boot(
        task: Task, image: str, box: DockerBox, run_args: Sequence[str], command: Sequence[str]
    ) -> DockerBox:
        name = box.container
        timezone = task.seed.user.timezone if task.seed is not None else "UTC"  # a world-less task has no persona
        await docker(
            "run",
            "-d",
            "--name",
            name,
            *run_args,
            "--cpus",
            BOX_CPUS,
            "--memory",
            BOX_MEMORY,
            "--user",
            "agent",
            "--workdir",
            str(BOX_WORKSPACE),
            "--env",
            f"TZ={timezone}",
            image,
            *command,
        )
        # Static binaries ignore TZ and read the zone from /etc/localtime.
        zone = f"/usr/share/zoneinfo/{timezone}"
        script = (
            'test -f "$1" || { echo "unknown timezone $2: the box has no $1" >&2; exit 1; }; '
            'ln -sf "$1" /etc/localtime && printf "%s\\n" "$2" > /etc/timezone'
        )
        await docker("exec", "-u", "root", name, "sh", "-c", script, "sh", zone, timezone)
        if task.seed is not None:  # a world-less task keeps the host's clock: no world sets it
            await box.set_clock(task.seed.now)
        if seed_dir(task).is_dir():
            await docker("cp", f"{seed_dir(task)}/.", f"{name}:{BOX_WORKSPACE}")
            # docker cp writes as root; the agent must own its workspace.
            await docker("exec", "-u", "root", name, "chown", "-R", "agent:agent", str(BOX_WORKSPACE))
        return box

    async def exec(
        self, argv: Sequence[str], env: Mapping[str, str], cwd: Path
    ) -> tuple[asyncio.subprocess.Process, str]:
        pid_file = f"/tmp/.px-pid-{secrets.token_hex(6)}"
        # Names only in argv (`-e K`): docker reads each value from its own environment, so trial
        # credentials never show up in the host process list.
        # The docker client reads its own PATH, HOME and DOCKER_* from that same environment, so a
        # trial value under one of those names would silently retarget the client (wrong binary,
        # config or daemon) instead of reaching the box.
        clash = sorted(k for k in env if k in DOCKER_CLIENT_ENV or k.startswith("DOCKER_"))
        if clash:
            raise BoxError(f"box env cannot set {', '.join(clash)}: the docker client itself reads it")
        env_args = [a for k in env for a in ("-e", k)]
        # setsid gives the command its own process group and records its id, so kill() can take
        # down the in-container tree; "$@" passes argv through untouched (no shell parsing of it).
        clocked = (CLOCK_BIN, str(CLOCK_FILE), *argv) if self.clock else tuple(argv)
        if self.desktop:
            clocked = (*DESKTOP_EXEC, *clocked)
        wrapped = ["setsid", "-w", "sh", "-c", 'echo $$ > "$0"; exec "$@"', pid_file, *clocked]
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            "-u",
            self.user,
            "-w",
            str(cwd),
            *env_args,
            self.container,
            *wrapped,
            env={**os.environ, **env},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            limit=STREAM_LINE_LIMIT,
        )
        return proc, pid_file

    async def spawn(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> ExecProc:
        proc, pid_file = await self.exec(argv, env, cwd)
        return ExecProc(proc, Killer(proc, self, pid_file))

    async def set_clock(self, now: datetime) -> None:
        """Moves the box's wall clock to `now`; it then runs forward in real time.

        Stored as an offset from real time, so every process in the box agrees on the instant and
        running processes pick the change up within px-clock's reload interval. A box whose
        processes do not run under px-clock (a device box) keeps real time, so this does nothing.
        """
        if not self.clock:
            return
        offset = (now - datetime.now(UTC)).total_seconds()
        script = f'printf "%s\\n" "$1" > {CLOCK_FILE}.new && mv {CLOCK_FILE}.new {CLOCK_FILE}'
        await docker("exec", "-u", "root", self.container, "sh", "-c", script, "sh", f"{offset:.6f}")

    async def write_file(self, path: Path, data: bytes) -> None:
        if not (path.is_relative_to(BOX_WORKSPACE) or path.is_relative_to(BOX_HOME)):
            raise ValueError(f"{path} is outside the box's workspace and home")
        script = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
        await docker("exec", "-i", "-u", self.user, self.container, "sh", "-c", script, "sh", str(path), stdin=data)

    async def close(self) -> None:
        with contextlib.suppress(BoxError):
            await docker("rm", "-f", self.container, timeout_s=60)


Box = HostBox | DockerBox


def device_box(info: HarnessInfo, isolation: Isolation) -> bool:
    """Whether a trial gets a device box: every remote trial of a harness that declares one.

    Such a product's agent always has a shell, whatever the trial requires: with no device of the
    trial's own it runs on the account's primary device, which every trial shares, so a file one
    trial writes there is waiting for the next. Other remote trials keep a host dir."""
    if isolation is not Isolation.REMOTE:
        return False
    match info.remote_box:
        case RemoteBox.HOST:
            return False
        case RemoteBox.DEVICE:
            return True
        case _:
            assert_never(info.remote_box)


class BoxKind(StrEnum):
    """The kinds of box a harness's trials can run in; each is probed, and can fail, on its own."""

    HOST = "host"
    DEVICE = "device"
    DESKTOP = "desktop"


def box_kind(requires: frozenset[Capability], info: HarnessInfo, isolation: Isolation) -> BoxKind:
    if not device_box(info, isolation):
        return BoxKind.HOST
    return BoxKind.DESKTOP if Capability.DESKTOP in requires else BoxKind.DEVICE


async def open_box(task: Task, info: HarnessInfo, isolation: Isolation, net: str | None) -> Box:
    """`net` is the run's `px-<run>` network; required for docker isolation, unused otherwise."""
    match isolation:
        case Isolation.DOCKER:
            if net is None:
                raise ValueError("docker isolation needs the run network (Sidecar.network)")
            return await DockerBox.create(task, await build_image(info), net)
        case Isolation.REMOTE if device_box(info, isolation):
            image = await build_desktop_image(info) if task.meta.desktop is not None else await build_image(info)
            return await DockerBox.create_device(task, image)
        case Isolation.HOST | Isolation.REMOTE:
            return HostBox.create(task, isolation)
        case _:
            assert_never(isolation)


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


def sha8(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def source_checkout() -> Path | None:
    """The tree images are built from, or None for an installed release, whose images come from PyPI."""
    source = PACKAGE_DIR.parent
    return source if (source / "pyproject.toml").is_file() else None


def build_digest() -> str:
    """What image tags carry: `pypi` for a release (immutable once published), else a source digest.

    That is sha8 over the files a wheel of the tree ships, plus `pyproject.toml` and `uv.lock` (the
    dependencies), so an edit to the world, meter or a worker always reaches the sidecar and the
    boxes instead of a stale image cached under an unchanged version string.
    """
    if (source := source_checkout()) is None:
        return "pypi"
    digest = hashlib.sha256()
    package = source / "parallax"
    files = sorted(p for p in package.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    for path in (*files, source / "pyproject.toml", source / "uv.lock"):
        digest.update(path.relative_to(source).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:8]


def base_image_tag() -> str:
    return f"parallax-base:{__version__}-{build_digest()}"


def box_image_tag(info: HarnessInfo) -> str:
    return f"parallax-box-{info.name}:{__version__}-{sha8(info.install + build_digest())}"


def desktop_image_tag(info: HarnessInfo) -> str:
    return box_image_tag(info) + "-desktop"


# The desktop layer carries an x86_64 driver. On another host architecture it and the box image under it are
# built for amd64 under their own tags, so the native images every other box uses are never replaced.
DESKTOP_PLATFORM = "linux/amd64"


def _foreign(platform_: str | None) -> str:
    """A tag suffix for an image built for a platform other than the host's; empty for the host's own."""
    if platform_ is None or platform_.endswith(("/amd64", "/x86_64")) and platform.machine() in ("x86_64", "AMD64"):
        return ""
    return "-" + platform_.rsplit("/", 1)[-1]


async def image_exists(tag: str) -> bool:
    try:
        done = await run(("docker", "image", "inspect", tag), timeout_s=60)
    except FileNotFoundError as exc:
        raise BoxError("docker CLI not found on PATH") from exc
    return done.code == 0


async def build_with_wheel(dockerfile: str, tag: str, *build_args: str, platform_: str | None = None) -> None:
    """`docker build` of one of the package Dockerfiles, with a parallax wheel as context: a fresh
    build of the tree in a checkout, else this release's wheel from PyPI.

    The context also carries `constraints.txt`, the versions the host runs, so the sidecar prices as
    the host does: an unpinned install once gave it a newer genai-prices than the host's, and the
    two priced the same model differently.
    """
    with tempfile.TemporaryDirectory(prefix="px-image-") as ctx:
        if (source := source_checkout()) is None:
            await fetch_release(Path(ctx))
        elif (built := await run(("uv", "build", "--wheel", "--out-dir", ctx, str(source)), timeout_s=600)).code:
            raise BoxError(f"uv build failed: {built.err.strip()[-2000:]}")
        # parallax itself is the wheel: an editable install's metadata can lag the version string
        pins = {
            f"{n}=={d.version}\n" for d in distributions() if (n := d.metadata["Name"]) not in (None, "parallax-evals")
        }
        (Path(ctx) / "constraints.txt").write_text("".join(sorted(pins)))
        shutil.copy(PACKAGE_DIR / "px_clock.c", ctx)
        args = [a for arg in build_args for a in ("--build-arg", arg)]
        target = ["--platform", platform_] if platform_ else []
        await docker("build", "-f", str(PACKAGE_DIR / dockerfile), *target, *args, "-t", tag, ctx, timeout_s=1800)


async def fetch_release(ctx: Path) -> None:
    """This version's wheel from PyPI, checked against its published sha256."""
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            listing = (await client.get(f"https://pypi.org/pypi/parallax-evals/{__version__}/json")).raise_for_status()
            wheel = next(u for u in listing.json()["urls"] if u["packagetype"] == "bdist_wheel")
            body = (await client.get(wheel["url"])).raise_for_status().content
    except (httpx.HTTPError, StopIteration) as exc:
        raise BoxError(f"no parallax-evals {__version__} wheel on PyPI to build images from: {exc!r}") from exc
    if hashlib.sha256(body).hexdigest() != wheel["digests"]["sha256"]:
        raise BoxError(f"{wheel['filename']} from PyPI does not match its published sha256")
    (ctx / wheel["filename"]).write_bytes(body)


async def build_runtime_image(platform_: str | None = None) -> str:
    """`parallax-runtime:<version>`: the base stage without parallax; docker's cache keeps it cheap."""
    tag = f"parallax-runtime:{__version__}{_foreign(platform_)}"
    target = ["--platform", platform_] if platform_ else []
    with tempfile.TemporaryDirectory(prefix="px-runtime-") as ctx:
        shutil.copy(PACKAGE_DIR / "px_clock.c", ctx)
        await docker(
            "build", "-f", str(PACKAGE_DIR / "base.Dockerfile"), *target, "--target", "runtime", "-t", tag, ctx,
            timeout_s=1800,
        )  # fmt: skip
    return tag


async def build_base_image() -> str:
    """`parallax-base:<version>-<src8>`: the sidecar image."""
    tag = base_image_tag()
    if not await image_exists(tag):
        await build_with_wheel("base.Dockerfile", tag)
    return tag


async def build_image(info: HarnessInfo, platform_: str | None = None) -> str:
    """`parallax-box-<name>:<version>-<sha8(install + source)>`; cached by tag."""
    tag = box_image_tag(info) + _foreign(platform_)
    if not await image_exists(tag):
        runtime = await build_runtime_image(platform_)
        await build_with_wheel(
            "box.Dockerfile", tag, f"RUNTIME={runtime}", f"HARNESS_INSTALL={info.install}", platform_=platform_
        )
    return tag


async def build_desktop_image(info: HarnessInfo) -> str:
    """Optional desktop layer; no change to the default image's install or tag formula."""
    tag = desktop_image_tag(info)
    if not await image_exists(tag):
        base = await build_image(info, DESKTOP_PLATFORM)
        with tempfile.TemporaryDirectory(prefix="px-desktop-") as ctx:
            await docker(
                "build",
                "--platform",
                DESKTOP_PLATFORM,
                "-f",
                str(PACKAGE_DIR / "desktop.Dockerfile"),
                "--build-arg",
                f"BOX_IMAGE={base}",
                "-t",
                tag,
                ctx,
                timeout_s=1800,
            )
    return tag


async def verify_image(task: Task) -> str:
    """The task's own `environment/Dockerfile` when present, else `parallax-verify:py3.13`."""
    env_dir = task.path / "environment"
    dockerfile = env_dir / "Dockerfile"
    if dockerfile.is_file():
        tag = f"parallax-verify-{task.id}:{sha8(dockerfile.read_text())}"
        if not await image_exists(tag):
            await docker("build", "-f", str(dockerfile), "-t", tag, str(env_dir), timeout_s=1800)
        return tag
    if not await image_exists(VERIFY_IMAGE):
        with tempfile.TemporaryDirectory(prefix="px-verify-") as ctx:
            await docker("build", "-f", str(PACKAGE_DIR / "verify.Dockerfile"), "-t", VERIFY_IMAGE, ctx, timeout_s=1800)
    return VERIFY_IMAGE


# ---------------------------------------------------------------------------
# sidecar
# ---------------------------------------------------------------------------


def wait_for_port(port: int, deadline: float) -> bool:
    """True once an HTTP server answers on `port`, whatever its status.

    A TCP connect is not enough: Docker's userland proxy accepts on the published port before the
    server inside the container listens, then drops the connection, so the first request failed.
    """
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)
            return True
        time.sleep(0.25)
    return False


class Sidecar:
    """Per-run world+meter container and the internal network boxes are confined to.

    Admin tokens and upstream keys reach the container by NAME (`docker run -e NAME`), so their
    values come from the runner's environment and never appear in an argv.
    """

    def __init__(self, world_admin_token: str, meter_admin_token: str) -> None:
        self.world_admin_token = world_admin_token
        self.meter_admin_token = meter_admin_token
        self.network: str | None = None
        self.container: str | None = None

    async def up(self, run_id: str) -> SidecarUrls:
        net = f"px-{run_id}"
        name = f"{net}-sidecar"
        image = await build_base_image()
        self.network = net
        await self.down()  # a resumed run reuses its id: clear what a killed attempt left behind
        self.network = net  # before the create: an interrupt mid-create must still leave it to `down`
        await docker("network", "create", "--internal", "--label", f"parallax.net={net}", net)
        env_names = [k for k in UPSTREAM_ENV if k in os.environ]
        serve = (
            f"python -m parallax.world serve --http 0.0.0.0:{WORLD_PORT} & "
            f"exec python -m parallax.meter 0.0.0.0:{METER_PORT}"
        )
        await self.run_detached(image, name, net, env_names, serve)
        self.container = name
        world_port = await self.host_port(WORLD_PORT)
        meter_port = await self.host_port(METER_PORT)
        deadline = time.monotonic() + READY_TIMEOUT_S
        for port in (world_port, meter_port):
            if not await asyncio.to_thread(wait_for_port, port, deadline):
                logs = await run(("docker", "logs", "--tail", "50", name), timeout_s=30)
                raise BoxError(f"sidecar port {port} not ready after {READY_TIMEOUT_S:.0f}s: {logs.err[-2000:]}")
        return SidecarUrls(
            world_internal=f"http://sidecar:{WORLD_PORT}/mcp",
            world_host=f"http://127.0.0.1:{world_port}/mcp",
            meter_internal=f"http://sidecar:{METER_PORT}",
            meter_host=f"http://127.0.0.1:{meter_port}",
        )

    async def run_detached(self, image: str, name: str, net: str, env_names: list[str], command: str) -> None:
        env = {**os.environ, WORLD_ADMIN_ENV: self.world_admin_token, METER_ADMIN_ENV: self.meter_admin_token}
        argv = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            f"parallax.net={net}",
            "--user",
            "world",
            "-p",
            f"127.0.0.1::{WORLD_PORT}",
            "-p",
            f"127.0.0.1::{METER_PORT}",
            *(a for k in (WORLD_ADMIN_ENV, METER_ADMIN_ENV, *env_names) for a in ("-e", k)),
            image,
            "sh",
            "-c",
            command,
        ]
        proc = await asyncio.create_subprocess_exec(*argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise BoxError(f"sidecar failed to start: {err.decode(errors='replace').strip()[-2000:]}")
        # Started on the default bridge (egress for the meter), then joined to the internal net.
        await docker("network", "connect", "--alias", "sidecar", net, name)

    async def host_port(self, port: int) -> int:
        if self.container is None:
            raise BoxError("sidecar is not running")
        out = await docker("port", self.container, f"{port}/tcp")
        return int(out.strip().splitlines()[0].rpartition(":")[2])

    async def down(self) -> None:
        """Removes every container on the run network (boxes included), then the network."""
        if self.network is None:
            return
        with contextlib.suppress(BoxError):
            ids = (await docker("ps", "-aq", "--filter", f"label=parallax.net={self.network}")).split()
            if ids:
                await docker("rm", "-f", *ids, timeout_s=120)
        try:
            await docker("network", "rm", self.network, timeout_s=60)
        except BoxError as exc:
            if "not found" not in str(exc):  # none yet is the normal case before `up` creates it
                logger.warning("box.network_rm network=%s: %s", self.network, exc)
        self.network = None
        self.container = None


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def tolerant_filter(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """The `data` filter, but a member it rejects (an agent-made symlink pointing outside the
    workspace, say) is skipped and logged instead of aborting the whole snapshot."""
    try:
        return tarfile.data_filter(member, dest)
    except tarfile.FilterError as exc:
        logger.warning("snapshot: skipped %s: %s", member.name, exc)
        return None


async def snapshot(box: Box, dest: Path) -> Path:
    """Writes `dest/workspace.tar.gz` and extracts it to `dest/workspace/`; returns that dir."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / "workspace.tar.gz"
    match box:
        case HostBox():
            await asyncio.to_thread(tar_dir, box.workspace, archive)
        case DockerBox():
            done = await run_bytes(("docker", "cp", f"{box.container}:{BOX_WORKSPACE}/.", "-"))
            await asyncio.to_thread(archive.write_bytes, gzip.compress(done))
        case _:
            assert_never(box)
    out = dest / "workspace"
    await asyncio.to_thread(extract, archive, out)
    return out


def tar_dir(src: Path, archive: Path) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src, arcname=".")


def extract(archive: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(out, filter=tolerant_filter)


async def run_bytes(argv: Sequence[str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise BoxError(f"{' '.join(argv[:2])} failed: {err.decode(errors='replace').strip()[-2000:]}")
    return out


# ---------------------------------------------------------------------------
# trusted verify
# ---------------------------------------------------------------------------

# `docker run` exits 125 when docker itself failed (bad image, daemon error): that is missing
# evidence, not an agent failure.
DOCKER_RUN_FAILED = 125
# Verify output is evidence kept with the trial, not a log dump.
VERIFY_OUTPUT_CHARS = 20_000


async def trusted_verify(
    task: Task, snapshot_dir: Path, checks: Sequence[CommandCheck], *, isolation: Isolation
) -> dict[str, CommandOutcome]:
    """Runs each command check over a fresh copy of the snapshot, never in the agent's box.

    docker: `--network none`, the snapshot mounted read-only and copied into a tmpfs.
    host/remote: a subprocess in a temp copy (dev only; reports label it `web_isolation=flags`).
    """
    match isolation:
        case Isolation.DOCKER:
            try:
                image = await verify_image(task)
            except BoxError as exc:
                return {c.id: CommandOutcome(exit=None, stdout="", stderr="", error=str(exc)) for c in checks}
            return {c.id: await verify_docker(image, snapshot_dir, c) for c in checks}
        case Isolation.HOST | Isolation.REMOTE:
            return {c.id: await verify_host(snapshot_dir, c) for c in checks}
        case _:
            assert_never(isolation)


def clip(text: str) -> str:
    return text if len(text) <= VERIFY_OUTPUT_CHARS else text[-VERIFY_OUTPUT_CHARS:]


async def verify_docker(image: str, snapshot_dir: Path, check: CommandCheck) -> CommandOutcome:
    name = f"px-verify-{secrets.token_hex(6)}"
    argv = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--name",
        name,
        "--network",
        "none",
        "--security-opt",
        "no-new-privileges",
        "-v",
        f"{snapshot_dir.resolve()}:/src:ro",
        "--tmpfs",
        "/workspace:rw,exec,size=2g",
        "-w",
        "/workspace",
        image,
        "sh",
        "-c",
        'cp -R /src/. /workspace/ && exec "$@"',
        "sh",
        *check.run,
    ]
    # Own process group so Killer can signal the docker client; the container itself is removed below.
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    code, out, err = await ExecProc(proc, Killer(proc, None, None)).communicate(None, check.timeout_s)
    if code is None:
        # Killing the docker client does not stop the container.
        with contextlib.suppress(BoxError):
            await docker("rm", "-f", name, timeout_s=60)
        return CommandOutcome(
            exit=None, stdout=clip(out), stderr=clip(err + f"\n[timeout after {check.timeout_s}s]"), error=None
        )
    if code == DOCKER_RUN_FAILED:
        return CommandOutcome(
            exit=None, stdout=clip(out), stderr=clip(err), error=f"verify container failed: {err.strip()[-500:]}"
        )
    return CommandOutcome(exit=code, stdout=clip(out), stderr=clip(err), error=None)


async def verify_host(snapshot_dir: Path, check: CommandCheck) -> CommandOutcome:
    with tempfile.TemporaryDirectory(prefix="px-verify-") as tmp:
        work = Path(tmp) / "workspace"
        await asyncio.to_thread(shutil.copytree, snapshot_dir, work, symlinks=True)
        box = HostBox(Path(tmp), Isolation.HOST)
        try:
            proc = await box.spawn(check.run, env={}, cwd=work)
        except (FileNotFoundError, PermissionError) as exc:
            return CommandOutcome(exit=None, stdout="", stderr="", error=f"verify command not runnable: {exc}")
        code, out, err = await proc.communicate(None, check.timeout_s)
    if code is None:
        return CommandOutcome(
            exit=None, stdout=clip(out), stderr=clip(err + f"\n[timeout after {check.timeout_s}s]"), error=None
        )
    return CommandOutcome(exit=code, stdout=clip(out), stderr=clip(err), error=None)
