"""Recurrence for the calendar: a hand-written RRULE subset plus per-occurrence exceptions.

Supported: FREQ=DAILY|WEEKLY, BYDAY, UNTIL, COUNT (no dateutil). Occurrences are expanded in the
event's own zone, so a 09:00 standup stays at 09:00 local across a DST change. An occurrence is
keyed by its original local date "YYYY-MM-DD", which is also its key in the event's `exceptions`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Annotated

from pydantic import AfterValidator, Field

from parallax.models import JsonObject
from parallax.world.services.common import get_str, opt_str, parse_iso, zone

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
MAX_EXPANSION_DAYS = 3 * 366  # bounds a window-less walk over an open-ended series


@dataclass(frozen=True)
class RRule:
    weekly: bool
    byday: frozenset[int]
    until: date | datetime | None
    count: int | None


def parse_rrule(text: str) -> RRule:
    parts = dict(p.split("=", 1) for p in text.removeprefix("RRULE:").split(";") if "=" in p)
    unknown = set(parts) - {"FREQ", "BYDAY", "UNTIL", "COUNT"}
    if unknown:
        raise ValueError(f"unsupported RRULE parts {sorted(unknown)}; use FREQ, BYDAY, UNTIL, COUNT")
    freq = parts.get("FREQ")
    if freq not in ("DAILY", "WEEKLY"):
        raise ValueError("FREQ must be DAILY or WEEKLY")
    days = [d for d in parts.get("BYDAY", "").split(",") if d]
    if any(d not in WEEKDAYS for d in days):
        raise ValueError(f"BYDAY values must be among {','.join(WEEKDAYS)}")
    return RRule(
        weekly=freq == "WEEKLY",
        byday=frozenset(WEEKDAYS.index(d) for d in days),
        until=_until(parts["UNTIL"]) if "UNTIL" in parts else None,
        count=int(parts["COUNT"]) if "COUNT" in parts else None,
    )


def _until(raw: str) -> date | datetime:
    if "T" in raw:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=zone("UTC"))
    return datetime.strptime(raw, "%Y%m%d").date()


def _check_rrule(text: str | None) -> str | None:
    if text is not None:
        parse_rrule(text)
    return text


RRuleText = Annotated[
    str | None,
    AfterValidator(_check_rrule),
    Field(description="Recurrence, e.g. FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10 (FREQ DAILY|WEEKLY, BYDAY, UNTIL, COUNT)"),
]


@dataclass(frozen=True)
class Occurrence:
    key: str  # original local date, the exception key
    start: datetime
    end: datetime
    moved: bool


def series(event: JsonObject) -> Iterator[tuple[str, datetime, datetime]]:
    """Every original occurrence (ignoring exceptions), in order."""
    tz = zone(get_str(event, "tz"))
    first = parse_iso(get_str(event, "start")).astimezone(tz)
    length = parse_iso(get_str(event, "end")) - parse_iso(get_str(event, "start"))
    text = opt_str(event, "rrule")
    if text is None:
        yield first.date().isoformat(), first, first + length
        return
    rule = parse_rrule(text)
    days = rule.byday or (frozenset({first.weekday()}) if rule.weekly else frozenset(range(7)))
    emitted = 0
    for offset in range(MAX_EXPANSION_DAYS):
        day = first.date() + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        start = datetime.combine(day, time(first.hour, first.minute, first.second), tzinfo=tz)
        if _past(rule, start) or (rule.count is not None and emitted >= rule.count):
            return
        emitted += 1
        yield day.isoformat(), start, start + length


def _past(rule: RRule, start: datetime) -> bool:
    match rule.until:
        case None:
            return False
        case datetime() as until:
            return start > until
        case date() as until:
            return start.date() > until


def occurrences(event: JsonObject, lo: datetime, hi: datetime) -> list[Occurrence]:
    exceptions = event.get("exceptions") or {}
    if not isinstance(exceptions, dict):
        raise TypeError(f"event {event.get('id')!r}: exceptions must be an object")
    # A moved occurrence can land in the window from anywhere in the series, so only a series
    # without moves may stop at the window's end.
    has_moves = any(isinstance(v, dict) and "start" in v for v in exceptions.values())
    out: list[Occurrence] = []
    for key, start, end in series(event):
        if start >= hi and not has_moves:
            break
        override = exceptions.get(key)
        if isinstance(override, dict) and override.get("cancelled") is True:
            continue
        moved = isinstance(override, dict)
        if isinstance(override, dict):
            start, end = parse_iso(get_str(override, "start")), parse_iso(get_str(override, "end"))
        if start < hi and end > lo:
            out.append(Occurrence(key=key, start=start, end=end, moved=moved))
    return out
