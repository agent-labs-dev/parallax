"""Analytics: product events with trends, funnels and saved insights, in the style of PostHog.

Tables:
- events:   {id, event, person, ts, properties{}}; `ts` carries a UTC offset
- insights: {id, name, description, kind: trend|funnel, event?, steps[]?, window_days?, interval?}

Every tool reads. A date range is a pair of calendar dates in the persona's time zone, both inclusive,
and a week starts on Monday, as in the PostHog UI. A funnel counts the people whose first step falls in
the range, then those who did each later step in order within `window_days` of their first step.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from typing import ClassVar, Literal

from pydantic import Field, JsonValue

from parallax.models import JsonObject
from parallax.world.service import Service, ServiceError, tool
from parallax.world.services.common import Args, get_str, json_list, parse_iso, require, zone

Interval = Literal["day", "week"]
NONE = "(none)"


class RangeArgs(Args):
    date_from: date = Field(description="First day, YYYY-MM-DD, in the user's time zone")
    date_to: date = Field(description="Last day, YYYY-MM-DD, inclusive")


class TrendArgs(RangeArgs):
    event: str = Field(description="Event name, e.g. signed_up")
    interval: Interval = "day"
    breakdown: str | None = Field(None, description="Event property to split the counts by, e.g. source")


class FunnelArgs(RangeArgs):
    steps: list[str] = Field(min_length=2, description="Event names in order")
    window_days: int = Field(14, ge=1, le=90, description="Days a person has from the first step to finish")
    breakdown: str | None = Field(None, description="Property of the first step's event to split people by")


class InsightArgs(RangeArgs):
    id: str = Field(description="Insight id")


class AnalyticsService(Service):
    name: ClassVar[str] = "analytics"
    tables: ClassVar[tuple[str, ...]] = ("events", "insights")

    @tool(mutates=False, description="List the tracked event names with their total volume and properties.")
    async def list_events(self, args: Args) -> JsonValue:
        volume = Counter(get_str(e, "event") for e in self._events())
        props: dict[str, set[str]] = {}
        for e in self._events():
            props.setdefault(get_str(e, "event"), set()).update(_props(e))
        events: list[JsonValue] = [
            {"name": n, "volume": volume[n], "properties": [*sorted(props[n])]} for n in sorted(volume)
        ]
        return {"events": events}

    @tool(mutates=False, description="List saved insights (trends and funnels) and how each is defined.")
    async def list_insights(self, args: Args) -> JsonValue:
        return {"insights": json_list(self.world.table("analytics.insights").values())}

    @tool(mutates=False, description="Run a saved insight over a date range.")
    async def run_insight(self, args: InsightArgs) -> JsonValue:
        insight = require(self.world, "analytics.insights", args.id, "insight")
        if insight.get("kind") == "funnel":
            steps = insight.get("steps")
            if not isinstance(steps, list):
                raise TypeError(f"insight {args.id!r}: a funnel needs steps")
            window = insight.get("window_days", 14)
            return {"insight": insight} | self._funnel(
                [str(s) for s in steps], args.date_from, args.date_to, int(window) if isinstance(window, int) else 14
            )
        interval = insight.get("interval", "day")
        return {"insight": insight} | self._trend(
            get_str(insight, "event"), args.date_from, args.date_to, "week" if interval == "week" else "day"
        )

    @tool(mutates=False, description="Count one event over a date range, by day or week, optionally split.")
    async def trend(self, args: TrendArgs) -> JsonValue:
        return self._trend(args.event, args.date_from, args.date_to, args.interval, args.breakdown)

    @tool(mutates=False, description="Count how many people went through a sequence of events.")
    async def funnel(self, args: FunnelArgs) -> JsonValue:
        return self._funnel(args.steps, args.date_from, args.date_to, args.window_days, args.breakdown)

    def _events(self) -> list[JsonObject]:
        return sorted(self.world.table("analytics.events").values(), key=lambda e: parse_iso(get_str(e, "ts")))

    def _day(self, event: JsonObject) -> date:
        return parse_iso(get_str(event, "ts")).astimezone(zone(self.world.persona.timezone)).date()

    def _known(self, name: str) -> None:
        if not any(e.get("event") == name for e in self._events()):
            raise ServiceError("not_found", f"no event named {name!r}; list_events shows the tracked names")

    def _trend(
        self, event: str, start: date, end: date, interval: Interval, breakdown: str | None = None
    ) -> JsonObject:
        _check_range(start, end)
        self._known(event)
        buckets: dict[str, Counter[str]] = {}
        day = start
        while day <= end:
            buckets.setdefault(_bucket(day, interval).isoformat(), Counter())
            day += timedelta(days=1)
        for e in self._events():
            if e.get("event") == event and start <= (day := self._day(e)) <= end:
                buckets[_bucket(day, interval).isoformat()][_split(e, breakdown)] += 1
        series: list[JsonValue] = []
        for p, c in buckets.items():
            point: JsonObject = {"period": p, "count": sum(c.values())}
            if breakdown:
                point["breakdown"] = {k: v for k, v in sorted(c.items())}
            series.append(point)
        total = sum(sum(c.values()) for c in buckets.values())
        return {"event": event, "interval": interval, "series": series, "total": total}

    def _funnel(
        self, steps: list[str], start: date, end: date, window: int, breakdown: str | None = None
    ) -> JsonObject:
        _check_range(start, end)
        for name in steps:
            self._known(name)
        events = self._events()
        firsts: dict[str, JsonObject] = {}
        for e in events:
            person = get_str(e, "person")
            if e.get("event") == steps[0] and person not in firsts and start <= self._day(e) <= end:
                firsts[person] = e
        counts: dict[str, list[int]] = {}
        for person, first in firsts.items():
            reached, at = 1, parse_iso(get_str(first, "ts"))
            deadline = at + timedelta(days=window)
            for name in steps[1:]:
                nxt = next(
                    (
                        parse_iso(get_str(e, "ts"))
                        for e in events
                        if e.get("person") == person
                        and e.get("event") == name
                        and at <= parse_iso(get_str(e, "ts")) <= deadline
                    ),
                    None,
                )
                if nxt is None:
                    break
                reached, at = reached + 1, nxt
            row = counts.setdefault(_split(first, breakdown), [0] * len(steps))
            for i in range(reached):
                row[i] += 1
        total = [sum(r[i] for r in counts.values()) for i in range(len(steps))]
        result: JsonObject = {"steps": _steps(steps, total), "window_days": window}
        if breakdown:
            result["breakdown"] = {k: _steps(steps, v) for k, v in sorted(counts.items())}
        return result


def _check_range(start: date, end: date) -> None:
    if end < start:
        raise ServiceError("validation", "date_to: must not be before date_from")


def _bucket(day: date, interval: Interval) -> date:
    return day - timedelta(days=day.weekday()) if interval == "week" else day


def _props(event: JsonObject) -> list[str]:
    props = event.get("properties")
    return list(props) if isinstance(props, dict) else []


def _split(event: JsonObject, prop: str | None) -> str:
    if prop is None:
        return "all"
    props = event.get("properties")
    value = props.get(prop) if isinstance(props, dict) else None
    return NONE if value is None else str(value)


def _steps(names: list[str], counts: list[int]) -> list[JsonValue]:
    first = counts[0]
    return [
        {"event": n, "count": c, "conversion_from_first": round(c / first, 4) if first else None}
        for n, c in zip(names, counts, strict=True)
    ]
