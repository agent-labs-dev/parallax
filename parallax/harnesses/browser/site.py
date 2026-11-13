"""Site health and retry classification for browser providers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from urllib.parse import urlsplit

import httpx

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529})
TRANSIENT_TRANSPORT: tuple[type[httpx.HTTPError], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
    httpx.DecodingError,
)


class NotStarted(RuntimeError):
    """A provider refused a start request without creating a run."""


class Unavailable(RuntimeError):
    """A provider or site failure can plausibly clear on a later attempt."""


def retryable_status(status: int | None) -> bool:
    return status in RETRYABLE_STATUS


def transient_transport(error: BaseException) -> bool:
    return isinstance(error, TRANSIENT_TRANSPORT)


def refusal(status: int | None) -> bool:
    return status is not None and status >= 400 and status not in RETRYABLE_STATUS


async def site_down(http: httpx.AsyncClient, url: str, *, timeout: float = 20.0) -> str | None:
    """Return an outage reason for a start URL, or ``None`` when it answers normally."""
    try:
        response = await http.get(url, follow_redirects=True, timeout=timeout)
    except httpx.TransportError as error:
        return f"site unavailable: {url} unreachable ({type(error).__name__})"
    if response.status_code >= 500:
        return f"site unavailable: {url} answered HTTP {response.status_code}"
    return None


def _origins(urls: Iterable[str]) -> dict[str, str]:
    return {parts.scheme + "://" + parts.netloc: url for url in urls if (parts := urlsplit(url)).netloc}


class SiteWatch:
    """Probe each task origin during a run and expose overlapping stalls."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        urls: Iterable[str],
        *,
        probe_seconds: float = 15.0,
        stall_seconds: float = 10.0,
        timeout: float | None = None,
    ) -> None:
        self.http = http
        self.starts = _origins(urls)
        self.probe_seconds = probe_seconds
        self.stall_seconds = stall_seconds
        self.timeout = timeout if timeout is not None else stall_seconds * 3
        self._stalls: dict[str, list[tuple[float, float, str]]] = {origin: [] for origin in self.starts}
        self._pending: dict[str, tuple[float, asyncio.Event]] = {}
        self._stop = asyncio.Event()

    async def run(self) -> None:
        await asyncio.gather(*(self._probe(origin, url) for origin, url in self.starts.items()))

    async def stop(self) -> None:
        self._stop.set()

    async def _probe(self, origin: str, url: str) -> None:
        while not self._stop.is_set():
            began = time.time()
            settled = asyncio.Event()
            self._pending[origin] = began, settled
            try:
                why: str | None = None
                try:
                    response = await self.http.get(url, follow_redirects=True, timeout=self.timeout)
                    if response.status_code >= 500:
                        why = f"answered HTTP {response.status_code}"
                except httpx.TransportError as error:
                    why = f"unreachable ({type(error).__name__})"
                ended = time.time()
                if why is None and ended - began > self.stall_seconds:
                    why = f"took {ended - began:.0f}s to answer"
                if why is not None:
                    self._stalls[origin].append((began, ended, why))
            finally:
                self._pending.pop(origin, None)
                settled.set()
            await asyncio.sleep(max(0.0, self.probe_seconds - (ended - began)))

    async def stalled(self, url: str, start: float, end: float) -> str | None:
        origin = next((key for key in self.starts if url.startswith(key)), None)
        if origin is None:
            return None
        if (pending := self._pending.get(origin)) is not None and pending[0] < end:
            await pending[1].wait()
        for began, ended, why in self._stalls.get(origin, ()):
            if began < end and ended > start:
                return f"site stalled: {url} {why}"
        return None
