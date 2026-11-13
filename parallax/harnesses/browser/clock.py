"""Timing for a browser attempt.

The agent clock starts when the browser session is ready and stops when the agent
has returned its answer. Browser setup and final cleanup remain visible in the
breakdown, but never silently change the timing basis.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from types import TracebackType
from typing import Literal, Self

from parallax.models import Timing

TIMING_BASIS = "session-ready-to-answer"
SESSION_INCLUDED = "session-included"
SESSION_OPEN_SECONDS = 300.0
SESSION_CLOSE_SECONDS = 300.0

type Phase = Literal["open", "agent", "close"]


class AttemptClock:
    """Track open, agent, and close phases without putting setup in agent time."""

    def __init__(self, *, bounded: bool = True) -> None:
        self.bounded = bounded
        self.began = time.monotonic()
        self.ready_at: float | None = None
        self.answered_at: float | None = None
        self.asked_at: float | None = None
        self.opened = 0.0
        self.closed = 0.0
        self._closed_at: float | None = None
        self._timeout: asyncio.Timeout | None = None

    async def __aenter__(self) -> Self:
        self.began = time.monotonic()
        _current.set(self)
        self._timeout = asyncio.timeout(SESSION_OPEN_SECONDS if self.bounded else None)
        await self._timeout.__aenter__()
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> bool | None:
        if self._timeout is None:
            return None
        return await self._timeout.__aexit__(kind, error, trace)

    def _bound(self, seconds: float | None) -> None:
        if self.bounded and self._timeout is not None and not self._timeout.expired():
            self._timeout.reschedule(None if seconds is None else asyncio.get_running_loop().time() + seconds)

    def ready(self, at: float | None = None) -> None:
        if self.ready_at is None:
            self.ready_at = time.monotonic() if at is None else at
            self._bound(None)

    def asked(self, at: float | None = None) -> None:
        if self.asked_at is None:
            self.asked_at = time.monotonic() if at is None else at

    def page_ready(self, at: float | None = None) -> None:
        if self.asked_at is not None:
            now = time.monotonic() if at is None else at
            self.opened += max(0.0, now - self.asked_at)
            self.asked_at = None

    def session_opened(self, seconds: float) -> None:
        self.opened += max(0.0, seconds)

    def answered(self, at: float | None = None) -> None:
        if self.answered_at is not None:
            return
        now = time.monotonic() if at is None else at
        self.page_ready(now)
        self.answered_at = now
        self._bound(SESSION_CLOSE_SECONDS)

    def closed_at(self, at: float | None = None) -> None:
        if self._closed_at is None:
            self._closed_at = time.monotonic() if at is None else at
            if self.answered_at is not None:
                self.closed = max(0.0, self._closed_at - self.answered_at)

    def expired(self) -> bool:
        return self._timeout.expired() if self._timeout is not None else False

    @property
    def phase(self) -> Phase:
        if self.ready_at is None:
            return "open"
        return "agent" if self.answered_at is None else "close"

    @property
    def open_seconds(self) -> float | None:
        return None if self.ready_at is None else round(max(0.0, self.ready_at - self.began + self.opened), 2)

    @property
    def agent_seconds(self) -> float:
        if self.ready_at is None:
            return 0.0
        now = time.monotonic() if self.answered_at is None else self.answered_at
        start = self.asked_at if self.asked_at is not None else now
        return (
            max(0.0, start - self.ready_at - self.opened)
            if self.answered_at is None
            else max(0.0, self.answered_at - self.ready_at - self.opened)
        )

    @property
    def close_seconds(self) -> float | None:
        return None if self._closed_at is None or self.answered_at is None else round(self.closed, 2)

    def timing(self) -> Timing:
        return Timing(
            basis=TIMING_BASIS if self.ready_at is not None else SESSION_INCLUDED,
            breakdown={
                "open": round(self.opened * 1000, 2),
                "agent": round(self.agent_seconds * 1000, 2),
                "close": round((self.close_seconds or 0.0) * 1000, 2),
            },
        )

    def seconds(self) -> float:
        return round(self.agent_seconds, 2)


_current: ContextVar[AttemptClock | None] = ContextVar("parallax_browser_attempt_clock", default=None)


def current() -> AttemptClock | None:
    return _current.get()


def session_ready(at: float | None = None) -> float:
    moment = time.monotonic() if at is None else at
    if (clock := _current.get()) is not None:
        clock.ready(moment)
    return moment


def session_answered(at: float | None = None) -> float:
    moment = time.monotonic() if at is None else at
    if (clock := _current.get()) is not None:
        clock.answered(moment)
    return moment


def setup_marks() -> tuple[Callable[[], None], Callable[[], Awaitable[None]]]:
    clock = _current.get()

    def asked() -> None:
        if clock is not None:
            clock.asked()

    async def page_ready() -> None:
        if clock is not None:
            clock.page_ready()

    return asked, page_ready


def elapsed() -> float:
    clock = _current.get()
    return 0.0 if clock is None else clock.seconds()
