"""Start and always tear down a throwaway self-hosted Langfuse for the smoke suite.

The stack belongs to the run that starts it: a unique compose project, fresh secrets, no volumes, and a
`down` in `finally`. `remaining()` reports any container still carrying the project's label, so a test can
prove the cleanup instead of assuming it.
"""

from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

import httpx

COMPOSE = Path(__file__).parent / "langfuse" / "compose.yml"
READY_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class Stack:
    project: str
    base_url: str
    public_key: str
    secret_key: str


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def compose(project: str, env: dict[str, str], *args: str) -> None:
    subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(COMPOSE), *args],
        env={**os.environ, **env},
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )


def remaining(project: str) -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.split()


def wait_ready(base_url: str, public_key: str, secret_key: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    last = "no reply"
    while time.monotonic() < deadline:
        try:
            # Health answers before the init project exists; an authenticated read proves both.
            reply = httpx.get(f"{base_url}/api/public/projects", auth=(public_key, secret_key), timeout=5)
            if reply.status_code == httpx.codes.OK:
                return
            last = f"http {reply.status_code}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(2)
    raise TimeoutError(f"self-hosted Langfuse not ready after {READY_TIMEOUT_S:.0f}s: {last}")


def _terminated(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


@contextmanager
def langfuse_stack() -> Iterator[Stack]:
    project = f"parallax-smoke-{secrets.token_hex(4)}"
    web_port = free_port()
    stack = Stack(
        project=project,
        base_url=f"http://localhost:{web_port}",
        public_key=f"pk-lf-{secrets.token_hex(8)}",
        secret_key=f"sk-lf-{secrets.token_hex(16)}",
    )
    env = {
        "PARALLAX_LF_WEB_PORT": str(web_port),
        "PARALLAX_LF_S3_PORT": str(free_port()),
        "PARALLAX_LF_PUBLIC_KEY": stack.public_key,
        "PARALLAX_LF_SECRET_KEY": stack.secret_key,
        "PARALLAX_LF_SALT": secrets.token_hex(16),
        "PARALLAX_LF_ENCRYPTION_KEY": secrets.token_hex(32),
        "PARALLAX_LF_NEXTAUTH_SECRET": secrets.token_hex(16),
        "PARALLAX_LF_PG_PASSWORD": secrets.token_hex(12),
        "PARALLAX_LF_CH_PASSWORD": secrets.token_hex(12),
        "PARALLAX_LF_S3_PASSWORD": secrets.token_hex(12),
        "PARALLAX_LF_REDIS_PASSWORD": secrets.token_hex(12),
    }
    # A stopped test run gets SIGTERM, which skips `finally`; as an exception it unwinds through it.
    previous = signal.signal(signal.SIGTERM, _terminated)
    try:
        compose(project, env, "up", "-d", "--wait", "--quiet-pull")
        wait_ready(stack.base_url, stack.public_key, stack.secret_key)
        yield stack
    finally:
        compose(project, env, "down", "-v", "--remove-orphans", "--timeout", "5")
        signal.signal(signal.SIGTERM, previous)
