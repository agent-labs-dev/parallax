"""Scheduler: the user's own jobs. The runner delivers each due job back to the agent as a new turn.

Tables:
- jobs: {id, kind, spec, tz, title, instruction, active, created}
  kind cron:  spec is a 5-field cron expression, evaluated in `tz`
  kind once:  spec is an ISO datetime with offset (a naive input is read in `tz`)
  kind watch: spec is "<service>: <condition>", e.g. "mail: from acme.example"
- runs: {id, job, at, ...}: past deliveries; read-only for the agent
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from croniter import croniter
from pydantic import Field, JsonValue

from parallax.models import JsonObject
from parallax.world.service import Service, ServiceError, tool
from parallax.world.services.common import Args, get_str, iso, json_list, localize, now_iso, require, zone

OWNERSHIP = "This is the user's scheduler; jobs created anywhere else will not run."
WATCHABLE = (
    "mail",
    "chat",
    "calendar",
    "contacts",
    "drive",
    "sheets",
    "tables",
    "tracker",
    "helpdesk",
    "payments",
    "wiki",
)
WATCH_SPEC = re.compile(rf"^({'|'.join(WATCHABLE)})\s*:\s*\S.*$", re.DOTALL)


class Kind(StrEnum):
    CRON = "cron"
    ONCE = "once"
    WATCH = "watch"


SPEC_HELP = (
    "cron: 5-field cron expression in tz, e.g. '0 8 * * 1-5'; once: ISO datetime, e.g. '2026-06-12T16:00'; "
    f"watch: '<service>: <condition>' where service is one of {', '.join(WATCHABLE)}"
)


class CreateArgs(Args):
    kind: Kind = Field(description="cron = recurring, once = a single time, watch = run when something happens")
    spec: str = Field(description=SPEC_HELP)
    tz: str | None = Field(None, description="IANA time zone for cron/once; defaults to the user's")
    title: str = Field(min_length=1)
    instruction: str = Field(min_length=1, description="What to do each time the job runs")


class ListArgs(Args):
    active: bool | None = Field(None, description="Only active (true) or paused (false) jobs")


class IdArgs(Args):
    id: str = Field(description="Job id")


class UpdateArgs(Args):
    id: str = Field(description="Job id")
    kind: Kind | None = None
    spec: str | None = Field(None, description=SPEC_HELP)
    tz: str | None = None
    title: str | None = None
    instruction: str | None = None
    active: bool | None = Field(None, description="false pauses the job, true resumes it")


class RunsArgs(Args):
    job: str | None = Field(None, description="Only runs of this job")


class SchedulerService(Service):
    name: ClassVar[str] = "scheduler"
    tables: ClassVar[tuple[str, ...]] = ("jobs", "runs")

    @tool(mutates=True, additive=True, description=f"Create a scheduled or event-triggered job. {OWNERSHIP}")
    async def create_job(self, args: CreateArgs) -> JsonValue:
        tz = args.tz or self.world.persona.timezone
        row: JsonObject = {
            "id": self.world.new_id("job_"),
            "kind": args.kind.value,
            "spec": self._spec(args.kind, args.spec, tz),
            "tz": tz,
            "title": args.title,
            "instruction": args.instruction,
            "active": True,
            "created": now_iso(self.world),
        }
        self.world.table("scheduler.jobs")[get_str(row, "id")] = row
        return self._view(row)

    @tool(mutates=False, description=f"List jobs with their next run time. {OWNERSHIP}")
    async def list_jobs(self, args: ListArgs) -> JsonValue:
        rows = [j for j in self.world.table("scheduler.jobs").values() if args.active in (None, j.get("active"))]
        return {"jobs": json_list(self._view(j) for j in rows)}

    @tool(mutates=False, description=f"Get one job. {OWNERSHIP}")
    async def get_job(self, args: IdArgs) -> JsonValue:
        return self._view(require(self.world, "scheduler.jobs", args.id, "job"))

    @tool(mutates=True, description=f"Change, pause or resume a job; omitted fields are kept. {OWNERSHIP}")
    async def update_job(self, args: UpdateArgs) -> JsonValue:
        row = dict(require(self.world, "scheduler.jobs", args.id, "job"))
        # Only a changed schedule is re-validated: pausing a one-off whose time has passed is fine.
        if (args.kind, args.spec, args.tz) != (None, None, None):
            kind = args.kind or Kind(get_str(row, "kind"))
            tz = args.tz or get_str(row, "tz")
            spec = args.spec if args.spec is not None else get_str(row, "spec")
            row |= {"kind": kind.value, "tz": tz, "spec": self._spec(kind, spec, tz)}
        edits: JsonObject = {"title": args.title, "instruction": args.instruction, "active": args.active}
        row |= {k: v for k, v in edits.items() if v is not None}
        self.world.table("scheduler.jobs")[args.id] = row
        return self._view(row)

    @tool(mutates=True, description=f"Delete a job permanently. {OWNERSHIP}")
    async def delete_job(self, args: IdArgs) -> JsonValue:
        require(self.world, "scheduler.jobs", args.id, "job")
        self.world.table("scheduler.jobs").pop(args.id)
        return {"deleted": args.id}

    @tool(mutates=False, description="The scheduler's current time and the user's time zone.")
    async def clock_now(self, args: Args) -> JsonValue:
        tz = self.world.persona.timezone
        return {"now": iso(self.world.now.astimezone(zone(tz))), "timezone": tz}

    @tool(mutates=False, description="Past runs of jobs.")
    async def list_runs(self, args: RunsArgs) -> JsonValue:
        rows = [r for r in self.world.table("scheduler.runs").values() if args.job in (None, r.get("job"))]
        return {"runs": json_list(rows)}

    def _spec(self, kind: Kind, spec: str, tz: str) -> str:
        zone(tz)
        match kind:
            case Kind.CRON:
                if len(spec.split()) != 5 or not croniter.is_valid(spec):
                    raise ServiceError("validation", f"spec: {spec!r} is not a 5-field cron expression")
                return " ".join(spec.split())
            case Kind.ONCE:
                return self._once(spec, tz)
            case Kind.WATCH:
                if not WATCH_SPEC.match(spec.strip()):
                    raise ServiceError(
                        "validation", f"spec: watch jobs use '<service>: <condition>' ({', '.join(WATCHABLE)})"
                    )
                return spec.strip()

    def _once(self, spec: str, tz: str) -> str:
        try:
            at = localize(datetime.fromisoformat(spec), tz)
        except ValueError as exc:
            raise ServiceError("validation", f"spec: {spec!r} is not an ISO datetime") from exc
        if at <= self.world.now:
            raise ServiceError("validation", f"spec: {iso(at)} is in the past (now {now_iso(self.world)})")
        return iso(at)

    def _view(self, row: JsonObject) -> JsonObject:
        return {**row, "next_run": self._next_run(row)}

    def _next_run(self, row: JsonObject) -> str | None:
        if row.get("active") is not True:
            return None
        match Kind(get_str(row, "kind")):
            case Kind.CRON:
                start = self.world.now.astimezone(zone(get_str(row, "tz")))
                return iso(croniter(get_str(row, "spec"), start).get_next(datetime))
            case Kind.ONCE:
                spec = get_str(row, "spec")
                return spec if datetime.fromisoformat(spec) > self.world.now else None
            case Kind.WATCH:
                return None
