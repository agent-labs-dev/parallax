"""Safe JSON subprocess transport for browser workers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class WorkerEventError(RuntimeError):
    def __init__(self, expected: str, message: Mapping[str, Any]) -> None:
        super().__init__(f"worker returned {message.get('event')!r}, expected {expected!r}")
        self.message = dict(message)


class JsonWorker:
    def __init__(self, command: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> None:
        self.command = tuple(command)
        self.env = dict(env)
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=self.cwd,
            start_new_session=True,
            limit=64 * 1024 * 1024,
        )

    async def send(self, message: Mapping[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("worker is not running")
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()

    async def receive(self, event: str) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("worker is not running")
        line = await self.process.stdout.readline()
        if not line:
            code = await self.process.wait()
            raise RuntimeError(f"worker exited {code} before {event}")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("worker emitted invalid JSON") from error
        if not isinstance(message, dict):
            raise RuntimeError("worker emitted a non-object event")
        if message.get("event") != event:
            raise WorkerEventError(event, message)
        return message

    async def stop(self, *, graceful: bool = True) -> None:
        process = self.process
        if process is None:
            return
        if graceful and process.returncode is None and process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.write(b"{}\n")
                await process.stdin.drain()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), 60)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 60)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    async def __aenter__(self) -> JsonWorker:
        await self.start()
        return self

    async def __aexit__(self, kind: object, error: object, trace: object) -> None:
        await asyncio.shield(self.stop(graceful=error is None))
