"""JSON-line bridge for a browser command supplied by the caller."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys


async def run(request: dict[str, object]) -> None:
    command = os.environ.get("PARALLAX_BROWSER_AGENT_COMMAND", "codex")
    env = dict(os.environ)
    cdp_url = request.get("cdp_url")
    if isinstance(cdp_url, str):
        env["BU_CDP_WS"] = cdp_url
        env["PARALLAX_BROWSER_CDP_URL"] = cdp_url
    argv = [*shlex.split(command), "exec", "--json", "--", str(request["goal"])]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, _ = await process.communicate()
    answer = stdout.decode(errors="replace").strip()
    emit(
        {"event": "result", "status": "done" if process.returncode == 0 else "error", "answer": answer, "step_log": []}
    )
    await asyncio.to_thread(sys.stdin.readline)
    emit({"event": "stopped", "stopped": True})


def emit(message: dict[str, object]) -> None:
    print(json.dumps(message, default=str), flush=True)


if __name__ == "__main__":
    asyncio.run(run(json.loads(sys.stdin.readline())))
