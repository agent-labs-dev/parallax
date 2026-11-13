"""Structural seams between slices.

Attribute members are declared as read-only properties so an implementation may
satisfy them with either a plain attribute or a property.

MCP SDK notes (mcp 2.0.0, verified against the installed package in wave 0):
- server: `mcp.server.lowlevel.Server(name, on_list_tools=..., on_call_tool=...)`; handlers are
  constructor callbacks taking `(ctx, params)`, not decorators as in 1.x.
- streamable HTTP: `Server.streamable_http_app(streamable_http_path="/mcp", host=...) -> Starlette`
  (or `mcp.server.streamable_http_manager.StreamableHTTPSessionManager` to mount it yourself).
  With `host` in 127.0.0.1/localhost it auto-enables DNS-rebinding protection, which rejects
  any other Host header (e.g. `sidecar:7801`): pass `transport_security` explicitly for docker.
- stdio: `mcp.server.stdio.stdio_server()`; client stdio: `mcp.client.stdio.stdio_client`.
- client: `mcp.client.streamable_http.streamable_http_client(url, http_client=...)` yields the
  transport streams for `mcp.ClientSession`; it takes an `httpx2.AsyncClient` (the `httpx2`
  package, not `httpx`), so auth headers go on that client. `mcp.Client` is the high-level wrapper.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from parallax.models import HarnessInfo, JsonObject, Persona, Timing, Trial, TurnInput, TurnOutput


class Proc(Protocol):
    # Monotonic time the process exited (or was killed); None until `communicate` returns. Output a
    # descendant keeps writing after it is not the process's time.
    exited_at: float | None

    async def communicate(
        self, stdin: bytes | None, timeout_s: float | None, *, quiet_s: float | None = None
    ) -> tuple[int | None, str, str]: ...

    async def kill(self) -> None: ...


class BoxHandle(Protocol):
    """Provided by box.py."""

    @property
    def workspace(self) -> Path: ...

    @property
    def home(self) -> Path: ...

    async def spawn(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path) -> Proc: ...

    async def write_file(self, path: Path, data: bytes) -> None: ...


class Harness(Protocol):
    @property
    def info(self) -> HarnessInfo: ...

    async def start(self, trial: Trial, box: BoxHandle) -> None: ...

    async def turn(self, t: TurnInput) -> TurnOutput: ...

    async def stop(self) -> None:
        """Idempotent; never deletes evidence."""
        ...

    async def version(self, box: BoxHandle) -> str: ...


# Optional harness hooks, read by the runner after `stop` and written into the trial's row. A harness
# implements any of them; one that does not leaves the row's field at its default, absent from the row.


@runtime_checkable
class TrialTiming(Protocol):
    """What the trial's agent time measures, and its phases (`TrialResult.timing_basis`, `timing_breakdown`)."""

    def trial_timing(self) -> Timing | None: ...


@runtime_checkable
class TrialCosts(Protocol):
    """Spend by component in USD (`TrialResult.cost_by_component`), every component of the trial's cost: the row's
    `cost_usd` is their sum. A component whose price is unknown is None; the row then omits it, and its `cost_usd` is
    unknown too."""

    def trial_costs(self) -> Mapping[str, float | None]: ...


@runtime_checkable
class TrialDetails(Protocol):
    """Secret-free facts the row carries for its plugin (`TrialResult.details`), such as a pooled lease's wait."""

    def trial_details(self) -> JsonObject: ...


class WorldState(Protocol):
    """What a Service sees of the world; implemented by world.core.World."""

    @property
    def now(self) -> datetime: ...

    @property
    def persona(self) -> Persona: ...

    def table(self, name: str) -> dict[str, JsonObject]:
        """Mutable id -> row view of table `name` ("svc.table"); changes surface in the diff."""
        ...

    def new_id(self, prefix: str) -> str:
        """Deterministic: prefix + per-world counter."""
        ...
