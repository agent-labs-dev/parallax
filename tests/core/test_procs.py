"""An owned child's whole process group stops with it: a grandchild never outlives the command that started it."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from parallax import procs

# A child that starts a grandchild, records its pid, and then waits forever.
FAMILY = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(600)
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed grandchild is reparented to init and reaped; until then it is a zombie, which is not running.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except FileNotFoundError:
        return False


def grandchild(pidfile: Path) -> int:
    deadline = time.monotonic() + 10
    while not pidfile.exists() or not pidfile.read_text():
        if time.monotonic() > deadline:
            raise AssertionError("the child never started its grandchild")
        time.sleep(0.02)
    return int(pidfile.read_text())


def gone(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def test_stop_kills_the_whole_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    proc = procs.spawn([sys.executable, "-c", FAMILY, str(pidfile)], dict(os.environ), tmp_path / "log")
    pid = grandchild(pidfile)
    procs.stop(proc, grace_s=0.2)
    assert gone(pid)
    assert oct((tmp_path / "log").stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_a_timed_out_or_cancelled_command_takes_its_group_with_it(tmp_path: Path, ending: str) -> None:
    pidfile = tmp_path / "pid"
    command = procs.run(
        [sys.executable, "-c", FAMILY, str(pidfile)], env=dict(os.environ), timeout_s=30 if ending == "cancel" else 1
    )
    task = asyncio.ensure_future(command)
    while not pidfile.exists() or not pidfile.read_text():
        await asyncio.sleep(0.02)
    pid = int(pidfile.read_text())
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(procs.ProcError, match="process group was killed"):
            await task
    assert gone(pid)


@pytest.mark.parametrize(("code", "raises"), [("print('ok')", False), ("import sys; sys.exit('broken')", True)])
async def test_a_finished_command_reports_its_exit_and_check_raises_its_stderr(code: str, raises: bool) -> None:
    done = await procs.run([sys.executable, "-c", code], env=dict(os.environ))
    if raises:
        with pytest.raises(procs.ProcError, match="exited 1: broken"):
            done.check()
    else:
        assert done.check().stdout == b"ok\n"
