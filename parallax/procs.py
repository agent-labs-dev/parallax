"""Child processes a harness or plugin owns: each started as its own process group, and the whole group stopped on
every exit, so a grandchild (a browser, a dev server) never outlives the trial that started it.

`spawn` and `stop` serve a long-lived child (a service a trial talks to); `run` serves a command whose output the
caller reads (a runner, a reset script), and kills the group when it times out or the caller is cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

TERM_GRACE_S = 5.0  # a stopped child's own teardown (closing sessions, releasing a lease) needs time
STDERR_TAIL = 2000  # characters of stderr a failed command's error carries: the reason is printed last


class ProcError(RuntimeError):
    pass


def spawn(argv: Sequence[str], env: Mapping[str, str], log: Path, cwd: Path | None = None) -> subprocess.Popen[bytes]:
    """A child in its own process group, its output going to a private (0600) log and nowhere else."""
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        return subprocess.Popen(  # noqa: S603  (argv is the caller's own command, never a shell string)
            list(argv),
            env=dict(env),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ProcError(f"command not found: {argv[0]}") from exc
    finally:
        os.close(fd)


def _signal_group(pid: int, sig: signal.Signals) -> None:
    # start_new_session made the child its group's leader, so its pid is the group id. The group is signalled even
    # when the leader has exited, or a child it started would survive it.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def stop(proc: subprocess.Popen[bytes], grace_s: float = TERM_GRACE_S, sig: signal.Signals = signal.SIGTERM) -> None:
    """Ask the child's whole group to stop with `sig`, then SIGKILL the group after `grace_s`. The group is killed
    even when the leader already exited, since its children may not have."""
    if proc.poll() is None:
        _signal_group(proc.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=grace_s)
    _signal_group(proc.pid, signal.SIGKILL)
    proc.wait()


@dataclass(frozen=True)
class Completed:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes

    def check(self) -> Completed:
        """Self when the command exited 0; raises `ProcError` with its stderr's tail otherwise."""
        if self.returncode != 0:
            tail = self.stderr.decode("utf-8", errors="replace").strip()[-STDERR_TAIL:]
            raise ProcError(f"{self.argv[0]} exited {self.returncode}: {tail}")
        return self


async def _kill(process: asyncio.subprocess.Process) -> None:
    _signal_group(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        await process.wait()


async def run(
    argv: Sequence[str],
    *,
    stdin: bytes = b"",
    env: Mapping[str, str],
    cwd: Path | None = None,
    timeout_s: float | None = None,
) -> Completed:
    """Run `argv` as its own group with `stdin`, and return its exit and output. `timeout_s` None waits as long as it
    takes. A timeout raises `ProcError`; a timeout or a cancel kills the whole group first, and a cancel is re-raised."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ProcError(f"command not found: {argv[0]}") from exc
    except OSError as exc:
        raise ProcError(f"{argv[0]} could not start: {exc}") from exc
    try:
        async with asyncio.timeout(timeout_s):
            stdout, stderr = await process.communicate(stdin)
    except TimeoutError as exc:
        await asyncio.shield(_kill(process))
        raise ProcError(f"{argv[0]} ran past {timeout_s:g}s; its process group was killed") from exc
    except BaseException:
        await asyncio.shield(_kill(process))
        raise
    # The group may hold children the leader left running; none outlives the command.
    _signal_group(process.pid, signal.SIGKILL)
    if process.returncode is None:
        raise ProcError(f"{argv[0]} closed its output but reported no exit status")
    return Completed(argv=tuple(argv), returncode=process.returncode, stdout=stdout, stderr=stderr)
