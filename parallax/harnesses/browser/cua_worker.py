"""Subprocess boundary for a CUA desktop runner.

The concrete driver is selected by environment, so Parallax can test the
protocol without installing a desktop stack on the evaluation runner.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys


async def run(request: dict[str, object]) -> None:
    driver = os.environ.get("PARALLAX_CUA_DRIVER", "cua-driver")
    codex = os.environ.get("PARALLAX_CUA_CODEX", "codex")
    if shutil.which(driver) is None or shutil.which(codex) is None:
        emit({"event": "result", "status": "unavailable", "answer": None, "error": "CUA dependencies are missing"})
        emit({"event": "stopped", "stopped": True})
        return
    # A driver implementation can opt into this worker contract. Keeping the
    # command boundary explicit prevents shell interpolation of task text.
    command = os.environ.get("PARALLAX_CUA_COMMAND")
    if command is None:
        emit({"event": "result", "status": "unavailable", "answer": None, "error": "CUA command is not configured"})
        emit({"event": "stopped", "stopped": True})
        return
    process = await asyncio.create_subprocess_exec(
        *command.split(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env=dict(os.environ),
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write((json.dumps(request) + "\n").encode())
    await process.stdin.drain()
    line = await process.stdout.readline()
    emit(json.loads(line) | {"event": "result"})
    await asyncio.to_thread(sys.stdin.readline)
    process.terminate()
    await process.wait()
    emit({"event": "stopped", "stopped": True})


def emit(message: dict[str, object]) -> None:
    print(json.dumps(message, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(run(json.loads(sys.stdin.readline())))
