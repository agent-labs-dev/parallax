"""World core: the stateful mock world one trial talks to.

The world owns every table of every mounted service, the frozen clock, the fault
schedule, the call log and the seed baseline the final state is diffed against.
Services only ever see it through the `WorldState` protocol.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, assert_never

from pydantic import JsonValue, ValidationError

from parallax.match import match_fields
from parallax.models import (
    CallError,
    Fault,
    FaultKind,
    Fire,
    JsonObject,
    Matcher,
    Persona,
    RowChange,
    State,
    StateDiff,
    TableDiff,
    WorldCall,
    WorldEvidence,
    WorldPatch,
    WorldSeed,
    WorldToolSpec,
)
from parallax.world.service import Service, ServiceError, ToolSpec

if TYPE_CHECKING:
    from parallax.models import Task

LOG_RESULT_LIMIT = 8 * 1024
RETRY_AFTER_S = 30
LOST_RESPONSE_MESSAGE = "The request timed out; the operation may or may not have completed."

# Seed data that is not a service table but that a service must read. They are loaded
# into these tables whenever the owning service is mounted, so the service reads them
# through `WorldState.table` like any other row and they appear in the evidence.
WEB_PAGES = "web.pages"  # one row per WorldSeed.web page, id = url
USER_RULES = "user.rules"  # one row per WorldSeed.user_rules entry, id = "rule<N>" in seed order


class WorldError(ValueError):
    """A seed, patch or control request that does not fit this world (an eval bug, never the agent's)."""


@dataclass(frozen=True)
class _Route:
    service: Service
    method: str


class World:
    def __init__(
        self,
        seed: WorldSeed,
        services: Sequence[str],
        faults: Sequence[Fault],
        *,
        registry: Mapping[str, type[Service]] | None = None,
    ) -> None:
        # `registry` exists so the core can be exercised with toy services; the real
        # catalogue is imported lazily to keep this module free of a hard dependency on it.
        classes = _resolve_services(services, registry)
        self._now: datetime = seed.now
        self._seed_now: datetime = seed.now
        self._persona = seed.user
        self._faults = tuple(faults)
        self._state: State = {f"{cls.name}.{t}": {} for cls in classes for t in _tables_of(cls)}
        self._load_seed(seed, {cls.name for cls in classes})
        self._baseline: State = copy.deepcopy(self._state)
        self._services = tuple(cls(self) for cls in classes)
        self._routes = {spec.name: _Route(svc, spec.name.removeprefix(f"{svc.name}_"))
                        for svc in self._services for spec in svc.specs()}  # fmt: skip
        self._specs = tuple(spec for svc in self._services for spec in svc.specs())
        self._validate_faults()
        self._turn = 1
        self._session = 1
        self._id_counter = 0
        self._tool_counts: Counter[str] = Counter()
        self._calls: list[WorldCall] = []
        self._internal_error: str | None = None
        self._missed_fires: list[str] = []
        self._touched: dict[str, dict[str, JsonObject]] | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def for_task(cls, task: Task, *, mount_defaults: bool | None = None) -> World:
        """Build the task world, mounting synthetic default services only for MCP tasks.

        A sidecar uses the host's explicit `mount_defaults` decision; local callers resolve
        extension defaults here. The MCP capability requirement applies in both cases.
        """
        from parallax import extensions
        from parallax.models import Capability
        from parallax.world.services import DEFAULT_SERVICES

        mcp = Capability.MCP in task.meta.requires
        if mount_defaults is None:
            mount_defaults = mcp and extensions.default_services(task)
        defaults = DEFAULT_SERVICES if mcp and mount_defaults else ()
        names = tuple(dict.fromkeys((*defaults, *task.meta.services)))
        return cls(task.world, names, task.meta.faults)

    # ---- WorldState -----------------------------------------------------------------

    @property
    def now(self) -> datetime:
        return self._now

    @property
    def persona(self) -> Persona:
        return self._persona

    def table(self, name: str) -> dict[str, JsonObject]:
        rows = self._state.get(name)
        if rows is None:
            raise KeyError(f"no table {name!r} in this world (mounted: {sorted(self._state)})")
        if self._touched is not None and name not in self._touched:
            self._touched[name] = copy.deepcopy(rows)
        return rows

    def new_id(self, prefix: str) -> str:
        # A counter shared by the whole world keeps ids deterministic for a given call
        # sequence; skipping taken ids keeps a seeded or patched "m3" from being reissued.
        while True:
            self._id_counter += 1
            candidate = f"{prefix}{self._id_counter}"
            if not self._id_taken(candidate):
                return candidate

    # ---- runtime ----------------------------------------------------------------------

    def services(self) -> tuple[str, ...]:
        """The mounted services, by name."""
        return tuple(svc.name for svc in self._services)

    def specs(self) -> tuple[ToolSpec, ...]:
        return self._specs

    def set_position(self, turn: int, session: int) -> None:
        if turn < 1 or session < 1:
            raise WorldError(f"position must be 1-based, got turn={turn} session={session}")
        self._turn, self._session = turn, session

    def advance_clock(self, to: datetime) -> None:
        if to.tzinfo is None:
            raise WorldError(f"clock target must be timezone-aware, got {to.isoformat()}")
        if to < self._now:
            raise WorldError(f"the world clock never runs backwards ({self._now.isoformat()} -> {to.isoformat()})")
        self._now = to

    def apply_patch(self, patch: WorldPatch) -> None:
        # The baseline absorbs every runner patch too: the diff (and every `unchanged`
        # check) must describe what the agent did, not what the scenario injected.
        _apply_strict(self._baseline, patch)
        _apply_lenient(self._state, patch)

    def find_job(self, match: dict[str, Matcher]) -> JsonObject | None:
        """The one scheduler job matching `match`, or None when zero or several match."""
        rows = self._state.get("scheduler.jobs")
        if rows is None:
            raise WorldError("find_job needs the scheduler service mounted")
        hits = [row for row in rows.values() if match_fields(row, match, self._persona.timezone, self._now)]
        return copy.deepcopy(hits[0]) if len(hits) == 1 else None

    def fire(self, fire: Fire) -> JsonObject | None:
        """Advance to the fire instant and select its job; a miss is recorded, not raised."""
        self.advance_clock(fire.at)
        job = self.find_job(fire.job)
        if job is None:
            self._missed_fires.append(fire.name)
        return job

    async def call(self, tool: str, args: JsonObject) -> tuple[bool, JsonValue]:
        # One call at a time per world: the seq, the fault counters and the touched-table
        # snapshot all assume calls do not interleave.
        async with self._lock:
            outcome = await self._dispatch(tool, args)
            self._log(tool, args, outcome)
        return outcome.ok, outcome.result

    def evidence(self) -> WorldEvidence:
        return WorldEvidence(
            seed_now=self._seed_now,
            seed_state=copy.deepcopy(self._baseline),
            final=copy.deepcopy(self._state),
            diff=diff_states(self._baseline, self._state),
            calls=tuple(self._calls),
            internal_error=self._internal_error,
            missed_fires=tuple(self._missed_fires),
            tool_schemas=tuple(
                WorldToolSpec(name=spec.name, description=spec.description, input_schema=spec.input_schema)
                for spec in self._specs
            ),
        )

    # ---- internals --------------------------------------------------------------------

    def _load_seed(self, seed: WorldSeed, mounted: set[str]) -> None:
        for service, tables in seed.services.items():
            if service not in mounted:
                raise WorldError(f"seed has rows for service {service!r}, which is not mounted")
            for table, rows in tables.items():
                _insert_rows(self._state, f"{service}.{table}", rows)
        if seed.web:
            _require_table(self._state, WEB_PAGES, "seed.web")
            _insert_rows(self._state, WEB_PAGES, [{"id": p.url, **p.model_dump(mode="json")} for p in seed.web])
        if seed.user_rules:
            _require_table(self._state, USER_RULES, "seed.user_rules")
            rules = [{"id": f"rule{i}", **r.model_dump(mode="json")} for i, r in enumerate(seed.user_rules, 1)]
            _insert_rows(self._state, USER_RULES, rules)

    def _validate_faults(self) -> None:
        for fault in self._faults:
            if fault.tool not in self._routes:
                raise WorldError(f"fault targets unknown tool {fault.tool!r}")
            if fault.on_call < 1 or fault.times < 0:
                raise WorldError(f"fault on {fault.tool}: on_call must be >= 1 and times >= 0")

    def _id_taken(self, candidate: str) -> bool:
        return any(candidate in rows for state in (self._state, self._baseline) for rows in state.values())

    def _fault_for(self, tool: str, n: int) -> FaultKind | None:
        for f in self._faults:
            if f.tool == tool and n >= f.on_call and (f.times == 0 or n < f.on_call + f.times):
                return f.kind
        return None

    async def _dispatch(self, tool: str, args: JsonObject) -> _Outcome:
        route = self._routes.get(tool)
        if route is None:
            return _Outcome.failed(CallError.VALIDATION, "validation", f"unknown tool {tool!r}")
        self._tool_counts[tool] += 1
        fault = self._fault_for(tool, self._tool_counts[tool])
        match fault:
            case None:
                return await self._run(route, args)
            case FaultKind.LOST_RESPONSE:
                committed = await self._run(route, args)
                if committed.error is CallError.INTERNAL:
                    return committed
                lost = _Outcome.failed(CallError.FAULT, "outcome_unknown", LOST_RESPONSE_MESSAGE, fault=fault)
                return lost.with_mutated(committed.mutated)
            case FaultKind.CORRUPT_SUCCESS:
                return self._corrupt_success(route, tool, args)
            case FaultKind.RATE_LIMITED | FaultKind.SERVER_ERROR | FaultKind.AUTH_EXPIRED | FaultKind.VALIDATION:
                return _fault_error(fault)
            case _:
                assert_never(fault)

    async def _run(self, route: _Route, args: JsonObject) -> _Outcome:
        touched: dict[str, dict[str, JsonObject]] = {}
        self._touched = touched
        try:
            result = await route.service.call(route.method, args)
        except ServiceError as exc:
            self._rollback()
            return _Outcome.failed(_CALL_ERRORS[exc.kind], exc.kind, exc.message)
        except Exception as exc:
            # A service raising anything else is an eval bug: the agent sees a generic
            # error, the trial is marked INVALID, and nothing it half-did survives.
            self._rollback()
            if self._internal_error is None:
                self._internal_error = f"{route.service.name}_{route.method}: {type(exc).__name__}: {exc}"
            return _Outcome.failed(CallError.INTERNAL, "internal", "Internal error.")
        finally:
            self._touched = None
        mutated = any(self._state[name] != before for name, before in touched.items())
        return _Outcome(ok=True, result=result, mutated=mutated)

    def _rollback(self) -> None:
        for name, before in (self._touched or {}).items():
            live = self._state[name]
            live.clear()
            live.update(before)

    def _corrupt_success(self, route: _Route, tool: str, args: JsonObject) -> _Outcome:
        try:
            route.service.tools[route.method].args_model.model_validate(args)
        except ValidationError:
            # A fake success for args the real service would reject would be a signal no
            # vendor ever sends; let the ordinary validation path answer instead.
            return _Outcome.failed(CallError.VALIDATION, "validation", f"invalid arguments for {tool}")
        # The fresh id resolves to not_found on any later read, which is the whole point.
        fake: JsonObject = {"id": self.new_id(f"{route.service.name}_"), **args}
        return _Outcome(ok=True, result=fake, mutated=False, fault=FaultKind.CORRUPT_SUCCESS)

    def _log(self, tool: str, args: JsonObject, outcome: _Outcome) -> None:
        route = self._routes.get(tool)
        self._calls.append(
            WorldCall(
                seq=len(self._calls) + 1,
                turn=self._turn,
                session=self._session,
                at=self._now,
                tool=tool,
                service=route.service.name if route else tool.partition("_")[0],
                args=copy.deepcopy(args),
                ok=outcome.ok,
                result=truncate(outcome.result),
                error=outcome.error,
                fault=outcome.fault,
                mutated=outcome.mutated,
            )
        )


@dataclass(frozen=True)
class _Outcome:
    ok: bool
    result: JsonValue
    mutated: bool
    error: CallError | None = None
    fault: FaultKind | None = None

    @staticmethod
    def failed(error: CallError, code: str, message: str, *, fault: FaultKind | None = None) -> _Outcome:
        return _Outcome(ok=False, result=_error_body(code, message), mutated=False, error=error, fault=fault)

    def with_mutated(self, mutated: bool) -> _Outcome:
        return _Outcome(ok=self.ok, result=self.result, mutated=mutated, error=self.error, fault=self.fault)


_CALL_ERRORS: dict[str, CallError] = {
    "validation": CallError.VALIDATION,
    "not_found": CallError.NOT_FOUND,
    "conflict": CallError.CONFLICT,
}


def _error_body(code: str, message: str, **extra: JsonValue) -> JsonObject:
    return {"error": {"code": code, "message": message, **extra}}


def _fault_error(kind: FaultKind) -> _Outcome:
    match kind:
        case FaultKind.RATE_LIMITED:
            body = _error_body("rate_limited", f"Rate limit exceeded. Retry after {RETRY_AFTER_S} seconds.",
                               status=429, retry_after_s=RETRY_AFTER_S)  # fmt: skip
        case FaultKind.SERVER_ERROR:
            body = _error_body("server_error", "The server encountered an error. Please try again.", status=500)
        case FaultKind.AUTH_EXPIRED:
            body = _error_body("auth_expired", "The access token has expired. Please re-authenticate.", status=401)
        case FaultKind.VALIDATION:
            # Names no field: any field we picked would come from the harness's own key
            # order, and a misleading field turns a recovery task into a key-order lottery.
            body = _error_body("validation", "Invalid request: one or more fields failed validation.", status=400)
        case FaultKind.LOST_RESPONSE | FaultKind.CORRUPT_SUCCESS:
            raise ValueError(f"{kind} is not an error-only fault")
        case _:
            assert_never(kind)
    return _Outcome(ok=False, result=body, mutated=False, error=CallError.FAULT, fault=kind)


def truncate(result: JsonValue, limit: int = LOG_RESULT_LIMIT) -> JsonValue:
    """The logged form of a result: unchanged when small, else a marked prefix of its JSON."""
    text = json.dumps(result, ensure_ascii=False)
    raw = text.encode()
    if len(raw) <= limit:
        return result
    return raw[:limit].decode(errors="ignore") + f"...[truncated {len(raw) - limit} bytes]"


# ---- state helpers ----------------------------------------------------------------------


def _resolve_services(names: Sequence[str], registry: Mapping[str, type[Service]] | None) -> tuple[type[Service], ...]:
    if registry is None:
        from parallax.world.services import SERVICES

        registry = SERVICES
    unknown = [n for n in names if n not in registry]
    if unknown:
        raise WorldError(f"unknown services {unknown}; known: {sorted(registry)}")
    return tuple(registry[n] for n in dict.fromkeys(names))


def _tables_of(cls: type[Service]) -> tuple[str, ...]:
    extra = {"web": ("pages",), "user": ("rules",)}.get(cls.name, ())
    return tuple(dict.fromkeys((*cls.tables, *extra)))


def _require_table(state: State, name: str, what: str) -> None:
    if name not in state:
        raise WorldError(f"{what} needs table {name!r}, which is not mounted")


def _insert_rows(state: State, name: str, rows: Sequence[JsonObject]) -> None:
    _require_table(state, name, "rows")
    table = state[name]
    for row in rows:
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise WorldError(f"{name}: every row needs a non-empty string id, got {row_id!r}")
        if row_id in table:
            raise WorldError(f"{name}: duplicate id {row_id!r}")
        table[row_id] = copy.deepcopy(row)


def patch_state(state: State, patch: WorldPatch) -> State:
    """A patched copy of `state` under the strict rules (every referenced row must exist)."""
    out = copy.deepcopy(state)
    _apply_strict(out, patch)
    return out


def _apply_strict(state: State, patch: WorldPatch) -> None:
    """Apply to the baseline, where every referenced row must be exactly as the patch expects."""
    for name, rows in patch.insert.items():
        _insert_rows(state, name, rows)
    for name, updates in patch.update.items():
        table = _existing(state, name)
        for row_id, fields in updates.items():
            if row_id not in table:
                raise WorldError(f"patch updates missing row {name}/{row_id}")
            table[row_id].update(copy.deepcopy(fields))
    for name, ids in patch.delete.items():
        table = _existing(state, name)
        for row_id in ids:
            if table.pop(row_id, None) is None:
                raise WorldError(f"patch deletes missing row {name}/{row_id}")
    for name, renames in patch.rename_ids.items():
        _rename(_existing(state, name), name, renames)


def _apply_lenient(state: State, patch: WorldPatch) -> None:
    """Apply to the live state after the baseline accepted the patch.

    The agent may already have deleted a row the patch updates or deletes; its action
    wins, so that row is skipped here (the baseline still records the patched value, so
    the diff shows the deletion).
    """
    for name, rows in patch.insert.items():
        _insert_rows(state, name, rows)
    for name, updates in patch.update.items():
        table = state[name]
        for row_id, fields in updates.items():
            if row_id in table:
                table[row_id].update(copy.deepcopy(fields))
    for name, ids in patch.delete.items():
        for row_id in ids:
            state[name].pop(row_id, None)
    for name, renames in patch.rename_ids.items():
        table = state[name]
        _rename(table, name, {old: new for old, new in renames.items() if old in table})


def _existing(state: State, name: str) -> dict[str, JsonObject]:
    _require_table(state, name, "patch")
    return state[name]


def _rename(table: dict[str, JsonObject], name: str, renames: Mapping[str, str]) -> None:
    for old, new in renames.items():
        if old not in table:
            raise WorldError(f"patch renames missing row {name}/{old}")
        if new in table:
            raise WorldError(f"patch renames {name}/{old} onto existing id {new!r}")
        row = table.pop(old)
        row["id"] = new
        table[new] = row


def diff_states(before: State, after: State) -> StateDiff:
    tables: dict[str, TableDiff] = {}
    for name in dict.fromkeys((*before, *after)):
        diff = _diff_table(before.get(name, {}), after.get(name, {}))
        if diff.added or diff.removed or diff.changed:
            tables[name] = diff
    return StateDiff(tables=tables)


def _diff_table(before: Mapping[str, JsonObject], after: Mapping[str, JsonObject]) -> TableDiff:
    changed = [
        RowChange(id=row_id, before=row, after=after[row_id], fields=_changed_fields(row, after[row_id]))
        for row_id, row in before.items()
        if row_id in after and row != after[row_id]
    ]
    return TableDiff(
        added=tuple(row for row_id, row in after.items() if row_id not in before),
        removed=tuple(row for row_id, row in before.items() if row_id not in after),
        changed=tuple(changed),
    )


def _changed_fields(before: JsonObject, after: JsonObject) -> tuple[str, ...]:
    keys = dict.fromkeys((*before, *after))
    return tuple(k for k in keys if k not in before or k not in after or before[k] != after[k])
