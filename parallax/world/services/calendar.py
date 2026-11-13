"""Calendar: events with a small recurrence subset and per-occurrence exceptions.

Tables:
- calendars: {id, name, owner, timezone}
- events:    {id, calendar, title, start, end, tz, attendees[], organizer, rrule, exceptions, status,
              description, location}
  `start`/`end` are the first occurrence (ISO with offset, in `tz`); `exceptions` maps an occurrence's
  original local date "YYYY-MM-DD" to {"cancelled": true} or {"start": iso, "end": iso}.

Recurrence lives in `recurrence.py`.
"""

from __future__ import annotations

import datetime as dt
from datetime import datetime
from typing import ClassVar

from pydantic import Field, JsonValue

from parallax.models import JsonObject
from parallax.world.service import Service, ServiceError, tool
from parallax.world.services.common import (
    Args,
    Email,
    get_str,
    iso,
    json_list,
    localize,
    opt_str,
    parse_iso,
    require,
    resolve,
    str_list,
    text_hit,
)
from parallax.world.services.recurrence import Occurrence, RRuleText, occurrences, series


class WindowArgs(Args):
    start: datetime = Field(description="Window start; times without an offset are in the user's time zone")
    end: datetime = Field(description="Window end")
    query: str | None = Field(None, description="Words that must appear in the title, description or attendees")


class FreeBusyArgs(Args):
    people: list[Email] = Field(min_length=1, description="Whose busy times to return")
    start: datetime
    end: datetime


class IdArgs(Args):
    id: str = Field(description="Event id")


class CreateArgs(Args):
    title: str = Field(min_length=1)
    start: datetime = Field(description="Start; without an offset it is read in `tz`")
    end: datetime
    tz: str | None = Field(None, description="IANA time zone; defaults to the user's")
    attendees: list[Email] = []
    rrule: RRuleText = None
    calendar: str | None = Field(None, description="Calendar id or name; defaults to the user's own")
    description: str = ""
    location: str = ""


class UpdateArgs(Args):
    id: str = Field(description="Event id; changes apply to every occurrence")
    title: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    attendees: list[Email] | None = Field(None, description="Replaces the attendee list")
    add_attendees: list[Email] = []
    remove_attendees: list[Email] = []
    rrule: RRuleText = None
    description: str | None = None
    location: str | None = None


class OccurrenceArgs(Args):
    event_id: str
    date: dt.date = Field(description="The occurrence's original date (YYYY-MM-DD)")


class MoveOccurrenceArgs(OccurrenceArgs):
    start: datetime = Field(description="New start; without an offset it is read in the event's time zone")
    end: datetime | None = Field(None, description="New end; defaults to keeping the duration")


class CalendarService(Service):
    name: ClassVar[str] = "calendar"
    tables: ClassVar[tuple[str, ...]] = ("calendars", "events")

    @tool(mutates=False, description="List event occurrences overlapping a time window, recurring ones expanded.")
    async def list_events(self, args: WindowArgs) -> JsonValue:
        lo, hi = self._window(args.start, args.end)
        found = [
            (occ, ev)
            for ev in self._live_events()
            if text_hit(ev, ("title", "description", "attendees"), args.query)
            for occ in occurrences(ev, lo, hi)
        ]
        found.sort(key=lambda pair: pair[0].start)
        return {"events": json_list(_occurrence_view(ev, occ) for occ, ev in found)}

    @tool(mutates=False, description="Busy intervals for each person (the user included) in a time window.")
    async def free_busy(self, args: FreeBusyArgs) -> JsonValue:
        lo, hi = self._window(args.start, args.end)
        busy: JsonObject = {}
        for person in args.people:
            spans = sorted(
                (occ.start, occ.end)
                for ev in self._live_events()
                if self._involves(ev, person)
                for occ in occurrences(ev, lo, hi)
            )
            busy[person] = json_list({"start": iso(s), "end": iso(e)} for s, e in spans)
        return {"busy": busy}

    @tool(mutates=False, description="Get one event (the whole series for a recurring event).")
    async def get_event(self, args: IdArgs) -> JsonValue:
        return require(self.world, "calendar.events", args.id, "event")

    @tool(mutates=True, additive=True, description="Create an event and invite attendees.")
    async def create_event(self, args: CreateArgs) -> JsonValue:
        tz = args.tz or self.world.persona.timezone
        start, end = localize(args.start, tz), localize(args.end, tz)
        _require_order(start, end)
        row: JsonObject = {
            "id": self.world.new_id("evt_"),
            "calendar": self._calendar_id(args.calendar),
            "title": args.title,
            "start": iso(start),
            "end": iso(end),
            "tz": tz,
            "attendees": json_list(args.attendees),
            "organizer": self.world.persona.email,
            "rrule": args.rrule,
            "exceptions": {},
            "status": "confirmed",
            "description": args.description,
            "location": args.location,
        }
        self.world.table("calendar.events")[get_str(row, "id")] = row
        return row

    @tool(mutates=True, description="Change an event; for a recurring event this changes the whole series.")
    async def update_event(self, args: UpdateArgs) -> JsonValue:
        row = dict(require(self.world, "calendar.events", args.id, "event"))
        tz = get_str(row, "tz")
        start = localize(args.start, tz) if args.start else parse_iso(get_str(row, "start"))
        end = localize(args.end, tz) if args.end else start + (_end(row) - parse_iso(get_str(row, "start")))
        _require_order(start, end)
        people = args.attendees if args.attendees is not None else str_list(row, "attendees")
        people = [p for p in people if p not in args.remove_attendees]
        people += [p for p in args.add_attendees if p not in people]
        row |= {"start": iso(start), "end": iso(end), "attendees": json_list(people)}
        edits = {"title": args.title, "rrule": args.rrule, "description": args.description, "location": args.location}
        row |= {k: v for k, v in edits.items() if v is not None}
        self.world.table("calendar.events")[args.id] = row
        return row

    @tool(mutates=True, description="Cancel an event; for a recurring event this cancels the whole series.")
    async def cancel_event(self, args: IdArgs) -> JsonValue:
        row = {**require(self.world, "calendar.events", args.id, "event"), "status": "cancelled"}
        self.world.table("calendar.events")[args.id] = row
        return row

    @tool(mutates=True, description="Move a single occurrence of a recurring event; the rest of the series is kept.")
    async def update_occurrence(self, args: MoveOccurrenceArgs) -> JsonValue:
        event, key, start, end = self._original(args)
        tz = get_str(event, "tz")
        new_start = localize(args.start, tz)
        new_end = localize(args.end, tz) if args.end else new_start + (end - start)
        _require_order(new_start, new_end)
        moved: JsonValue = (
            None if (new_start, new_end) == (start, end) else {"start": iso(new_start), "end": iso(new_end)}
        )
        return self._set_exception(event, key, moved)

    @tool(mutates=True, description="Cancel a single occurrence of a recurring event; the rest of the series is kept.")
    async def cancel_occurrence(self, args: OccurrenceArgs) -> JsonValue:
        event, key, _, _ = self._original(args)
        return self._set_exception(event, key, {"cancelled": True})

    @tool(mutates=True, description="Undo any change to a single occurrence, restoring it to the series' time.")
    async def restore_occurrence(self, args: OccurrenceArgs) -> JsonValue:
        event, key, _, _ = self._original(args)
        return self._set_exception(event, key, None)

    def _window(self, start: datetime, end: datetime) -> tuple[datetime, datetime]:
        lo, hi = localize(start, self.world.persona.timezone), localize(end, self.world.persona.timezone)
        _require_order(lo, hi)
        return lo, hi

    def _live_events(self) -> list[JsonObject]:
        return [e for e in self.world.table("calendar.events").values() if e.get("status") != "cancelled"]

    def _involves(self, event: JsonObject, person: str) -> bool:
        owner = self.world.table("calendar.calendars").get(get_str(event, "calendar"), {}).get("owner")
        people = {p.casefold() for p in [*str_list(event, "attendees"), opt_str(event, "organizer") or ""]}
        return person.casefold() in people or (isinstance(owner, str) and owner.casefold() == person.casefold())

    def _calendar_id(self, ref: str | None) -> str:
        if ref is not None:
            return get_str(resolve(self.world, "calendar.calendars", ref, "calendar"), "id")
        own = [c for c in self.world.table("calendar.calendars").values() if c.get("owner") == self.world.persona.email]
        return get_str(own[0], "id") if own else "primary"

    def _original(self, args: OccurrenceArgs) -> tuple[JsonObject, str, datetime, datetime]:
        event = require(self.world, "calendar.events", args.event_id, "event")
        key = args.date.isoformat()
        for k, start, end in series(event):
            if k == key:
                return event, key, start, end
            if k > key:
                break
        raise ServiceError("not_found", f"event {args.event_id} has no occurrence on {key}")

    def _set_exception(self, event: JsonObject, key: str, value: JsonValue) -> JsonObject:
        current = event.get("exceptions") or {}
        exceptions: JsonObject = {k: v for k, v in (current.items() if isinstance(current, dict) else ()) if k != key}
        if value is not None:
            exceptions[key] = value
        row = {**event, "exceptions": exceptions}
        self.world.table("calendar.events")[get_str(event, "id")] = row
        return row


def _end(row: JsonObject) -> datetime:
    return parse_iso(get_str(row, "end"))


def _require_order(start: datetime, end: datetime) -> None:
    if end <= start:
        raise ServiceError("validation", "end: must be after start")


def _occurrence_view(event: JsonObject, occ: Occurrence) -> JsonObject:
    recurring = event.get("rrule") is not None
    return {
        "id": event.get("id"),
        "occurrence": occ.key if recurring else None,
        "title": event.get("title"),
        "start": iso(occ.start),
        "end": iso(occ.end),
        "attendees": event.get("attendees", []),
        "calendar": event.get("calendar"),
        "location": event.get("location", ""),
        "recurring": recurring,
        "moved": occ.moved,
    }
