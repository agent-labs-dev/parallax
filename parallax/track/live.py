"""A run's progress, live: each trial start and finish is sent to Langfuse the moment it happens.

The sealed upload (`track.upload`) stays the record of a run; the feed is only for watching one. It writes small
events under the run's session in their own environment, so they are never experiment items and the upload's
verification never sees them, and it writes nothing into the run directory, whose files the seal hashes. A feed
that cannot reach Langfuse logs and goes quiet: watching a run must never fail it.

`progress` turns the events back into what a person watching wants: how far along, how it is going, and when it
should finish. `parallax watch` serves it as a page.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from langfuse import Langfuse, propagate_attributes

from parallax.models import TrialResult, Verdict

logger = logging.getLogger(__name__)

LIVE_ENVIRONMENT = "parallax-live"
OFF_ENV = "PARALLAX_LIVE"  # "0" turns the feed off
RUN_EVENT = "parallax.run"
STARTED_EVENT = "parallax.trial.started"
FINISHED_EVENT = "parallax.trial"
PAYLOAD = "payload"  # every event's fields travel as one JSON metadata value, read back untruncated
EVENTS = (RUN_EVENT, STARTED_EVENT, FINISHED_EVENT)


def _now() -> str:
    # Microseconds: a resume can start within the second its previous run ended, and the epochs must not tie.
    return datetime.now(UTC).isoformat(timespec="microseconds")


class LiveFeed:
    """Sends one run's events. Every method swallows its own failure after logging it once."""

    def __init__(self, client: Langfuse, run_id: str) -> None:
        self.client = client
        self.run_id = run_id
        self._failed = False

    @classmethod
    def open(cls, run_id: str) -> LiveFeed | None:
        """A feed for the project the run uploads to, or None when the feed is off or Langfuse is not set up."""
        if os.environ.get(OFF_ENV) == "0":
            return None
        from parallax.track.sink import connect

        try:
            client, _project = connect(write=True)
        except Exception as error:  # noqa: BLE001 - the feed is optional
            logger.warning("live feed off: %s", error)
            return None
        return cls(client, run_id)

    def _send(self, name: str, payload: Mapping[str, Any]) -> None:
        if self._failed:
            return
        try:
            with propagate_attributes(session_id=self.run_id, environment=LIVE_ENVIRONMENT, trace_name=name):
                self.client.create_event(name=name, metadata={PAYLOAD: json.dumps(payload, default=str)})
        except Exception as error:  # noqa: BLE001 - the feed is optional
            self._failed = True
            logger.warning("live feed stopped: %s", error)

    def run_started(
        self, *, total: int, already_done: int, concurrency: int, labels: Sequence[str], kept: Sequence[str]
    ) -> None:
        """`kept`: the keys of the `already_done` trials, kept from an earlier start and not run again."""
        self._send(
            RUN_EVENT,
            {
                "state": "running",
                "at": _now(),
                "total": total,
                "already_done": already_done,
                "concurrency": concurrency,
                "labels": list(labels),
                "kept": list(kept),
            },
        )

    def run_finished(self) -> None:
        self._send(RUN_EVENT, {"state": "finished", "at": _now()})
        self.flush()

    def trial_started(self, *, key: str, task_id: str, label: str, repeat: int, attempt: int) -> None:
        self._send(
            STARTED_EVENT,
            {"at": _now(), "key": key, "task": task_id, "label": label, "repeat": repeat, "attempt": attempt},
        )

    def trial_finished(self, result: TrialResult, label: str, *, retry_pending: bool = False) -> None:
        """`retry_pending`: the runner will run this trial again, so it is not done yet."""
        self._send(
            FINISHED_EVENT,
            {
                "at": _now(),
                "key": result.key,
                "task": result.task_id,
                "label": label,
                "repeat": result.repeat,
                "attempt": result.attempt,
                "verdict": result.verdict.value,
                "reason": result.invalid_reason or result.na_reason,
                "wall_s": result.wall_ms / 1000 if result.wall_ms is not None else None,
                "cost_usd": result.cost_usd,
                "retry_pending": retry_pending,
            },
        )

    def flush(self) -> None:
        try:
            self.client.flush()
        except Exception as error:  # noqa: BLE001 - the feed is optional
            logger.warning("live feed flush failed: %s", error)


def label(harness: str, model: str | None) -> str:
    return f"{harness} {model}" if model else harness


# ---------------------------------------------------------------------------
# reading the events back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    name: str
    session: str
    at: datetime
    payload: dict[str, Any]


class LiveSource:
    """Reads live events from Langfuse's v2 observations API, the one read path Langfuse serves in real time."""

    def __init__(self, base_url: str, public_key: str, secret_key: str) -> None:
        self.http = httpx.Client(base_url=base_url, auth=(public_key, secret_key), timeout=30)

    @classmethod
    def from_env(cls) -> LiveSource:
        base_url = (os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST") or "").rstrip("/")
        public_key, secret_key = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
        if not base_url or not public_key or not secret_key:
            from parallax.track.sink import TrackError

            raise TrackError("set LANGFUSE_BASE_URL, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
        from parallax.track.sink import PROJECT_ENV, connect

        if os.environ.get(PROJECT_ENV):
            connect(write=False)  # the keys must be the named project's, as every other tracking command checks
        return cls(base_url, public_key, secret_key)

    def events(self, *, since: datetime, session: str | None = None, name: str | None = None) -> Iterator[Event]:
        params: dict[str, Any] = {
            "fromStartTime": since.isoformat(),
            "toStartTime": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "environment": LIVE_ENVIRONMENT,
            "fields": "core,basic,metadata",
            "expandMetadata": PAYLOAD,
            "limit": 1000,
        }
        if session:
            params["sessionId"] = session
        if name:
            params["name"] = name
        while True:
            response = self.http.get("/api/public/v2/observations", params=params)
            response.raise_for_status()
            body = response.json()
            for row in body.get("data", []):
                if row.get("name") in EVENTS and (event := _event(row)) is not None:
                    yield event
            cursor = (body.get("meta") or {}).get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor


def _event(row: Mapping[str, Any]) -> Event | None:
    # Sent as a JSON string; the v2 API hands an expanded value back parsed.
    raw = (row.get("metadata") or {}).get(PAYLOAD)
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict) or not row.get("sessionId"):
        return None
    at = payload.get("at") or row.get("startTime")
    if not isinstance(at, str):
        return None
    return Event(name=row["name"], session=row["sessionId"], at=datetime.fromisoformat(at), payload=payload)


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------


@dataclass
class _Arm:
    done: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    pass_walls: list[float] = field(default_factory=list)
    costs: list[float | None] = field(default_factory=list)


def _seconds(delta: timedelta) -> float:
    return round(delta.total_seconds(), 1)


def progress(events: Iterable[Event], *, now: datetime) -> dict[str, Any]:
    """One run's state from its events: counts, pass rates, cost, the trials in flight and an ETA.

    A retried trial counts once, by its latest attempt. The ETA divides what is left by the rate trials have
    finished since the run (or its latest resume) started, so it is only known once one trial has finished."""
    ordered = sorted(events, key=lambda e: e.at)
    starts = [e for e in ordered if e.name == RUN_EVENT and e.payload.get("state") == "running"]
    start = starts[-1] if starts else None
    # Only the current start's end counts: a run interrupted and then resumed is running again.
    finished_state = next(
        (
            e
            for e in reversed(ordered)
            if e.name == RUN_EVENT and e.payload.get("state") == "finished" and start is not None and e.at >= start.at
        ),
        None,
    )
    already = start.payload.get("already_done", 0) if start else 0
    kept_keys = set(start.payload.get("kept", [])) if start else set()
    current = [e for e in ordered if start is None or e.at >= start.at]

    def last_attempts(source: Iterable[Event]) -> dict[str, Event]:
        found: dict[str, Event] = {}
        for e in source:
            if e.name == FINISHED_EVENT:
                key = e.payload["key"]
                if key not in found or e.payload.get("attempt", 0) >= found[key].payload.get("attempt", 0):
                    found[key] = e
        return found

    # The start names the trials it kept as done; each one's result, when the feed saw it, is its last finish
    # before the start. A kept trial the feed never saw stays unseen.
    before = last_attempts(e for e in ordered if start is not None and e.at < start.at)
    kept = {k: e for k, e in before.items() if k in kept_keys}
    finished = last_attempts(current)
    running = {(e.payload["key"], e.payload.get("attempt", 0)): e for e in current if e.name == STARTED_EVENT}
    for e in current:
        if e.name == FINISHED_EVENT:
            running.pop((e.payload["key"], e.payload.get("attempt", 0)), None)
    if finished_state is not None:
        running = {}  # a run that ended has nothing in flight: a trial cut off by it never sends a finish
    for key, attempt in running:
        # A retry in flight replaces its earlier attempt's result until it finishes.
        if key in finished and finished[key].payload.get("attempt", 0) < attempt:
            del finished[key]
    # A result the runner will retry is waiting, not done, until its next attempt starts (or the run ends).
    waiting = (
        {} if finished_state is not None else {k: e for k, e in finished.items() if e.payload.get("retry_pending")}
    )
    for key in waiting:
        del finished[key]
    finished_here = finished.keys()
    latest = {**kept, **finished}

    arms: dict[str, _Arm] = {}
    verdicts: dict[str, int] = {}
    costs: list[float | None] = []
    for e in latest.values():
        p = e.payload
        arm = arms.setdefault(p["label"], _Arm())
        arm.done += 1
        arm.verdicts[p["verdict"]] = arm.verdicts.get(p["verdict"], 0) + 1
        verdicts[p["verdict"]] = verdicts.get(p["verdict"], 0) + 1
        if p["verdict"] == Verdict.PASS.value and p.get("wall_s") is not None:
            arm.pass_walls.append(p["wall_s"])
        arm.costs.append(p.get("cost_usd"))
        costs.append(p.get("cost_usd"))

    total = start.payload["total"] if start else None
    # Kept trials count even when the feed never saw them (an older Parallax, the feed off).
    done = already + len(finished)
    elapsed = (now if finished_state is None else finished_state.at) - start.at if start else None
    eta_s = None
    if start and total is not None and finished_state is None and finished_here and elapsed:
        rate = len(finished_here) / elapsed.total_seconds()
        eta_s = round(max(total - done, 0) / rate) if rate > 0 else None
    # Done trials with no event: their verdicts and costs are unknown, so no rate or cost covers the whole run.
    unseen = done - len(latest)

    def known(values: Sequence[float | None]) -> list[float] | None:
        """Every cost, or None when any trial's is unknown: a partial sum is not the run's cost."""
        if unseen or any(v is None for v in values):
            return None
        return [v for v in values if v is not None]

    def rate(counts: Mapping[str, int]) -> float | None:
        if unseen:
            return None
        graded = counts.get(Verdict.PASS.value, 0) + counts.get(Verdict.FAIL.value, 0)
        return round(counts.get(Verdict.PASS.value, 0) / graded, 4) if graded else None

    return {
        "state": "finished" if finished_state else ("running" if start else "unknown"),
        "started_at": start.at.isoformat() if start else None,
        "elapsed_s": _seconds(elapsed) if elapsed is not None else None,
        "eta_s": eta_s,
        "eta_at": (now + timedelta(seconds=eta_s)).isoformat(timespec="seconds") if eta_s is not None else None,
        "total": total,
        "done": done,
        "unseen": unseen,
        "concurrency": start.payload.get("concurrency") if start else None,
        "verdicts": verdicts,
        "pass_rate": rate(verdicts),
        "cost_usd": round(sum(summed), 6) if (summed := known(costs)) is not None else None,
        "cost_unknown": sum(v is None for v in costs),
        "arms": {
            name: {
                "done": arm.done,
                "verdicts": arm.verdicts,
                "pass_rate": rate(arm.verdicts),
                "median_pass_s": round(statistics.median(arm.pass_walls), 1) if arm.pass_walls else None,
                "mean_cost_usd": round(statistics.fmean(c), 6) if (c := known(arm.costs)) else None,
            }
            for name, arm in sorted(arms.items())
        },
        "running": [{**e.payload, "for_s": _seconds(now - e.at)} for e in sorted(running.values(), key=lambda e: e.at)]
        + [
            {**e.payload, "waiting": True, "attempt": e.payload.get("attempt", 0) + 1, "for_s": _seconds(now - e.at)}
            for e in sorted(waiting.values(), key=lambda e: e.at)
        ],
        "recent": [e.payload for e in sorted(latest.values(), key=lambda e: e.at, reverse=True)[:50]],
    }


def runs(events: Iterable[Event]) -> list[dict[str, Any]]:
    """Every run with a start event, newest first."""
    seen: dict[str, Event] = {}
    for e in events:
        if (
            e.name == RUN_EVENT
            and e.payload.get("state") == "running"
            and (e.session not in seen or e.at > seen[e.session].at)
        ):
            seen[e.session] = e
    return [
        {"run_id": e.session, "started_at": e.at.isoformat(), "total": e.payload.get("total"),
         "labels": e.payload.get("labels", [])}
        for e in sorted(seen.values(), key=lambda e: e.at, reverse=True)
    ]  # fmt: skip
