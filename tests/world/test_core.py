"""World core behaviour: faults, diffs, patches, positions, fires, internal errors."""

from __future__ import annotations

from datetime import datetime

import pytest
from parallax.models import CallError, Fault, FaultKind, Fire, WorldPatch, WorldSeed
from parallax.world.core import LOST_RESPONSE_MESSAGE, World, WorldError, truncate
from parallax.world.service import Service, ServiceError, tool
from pydantic import BaseModel, JsonValue

NOW = "2026-06-03T09:00:00+01:00"


class AddArgs(BaseModel):
    text: str


class IdArgs(BaseModel):
    id: str


class EditArgs(BaseModel):
    id: str
    text: str


class Notes(Service):
    name = "notes"
    tables = ("items",)

    @tool(mutates=True, description="Add a note.")
    async def add(self, args: AddArgs) -> JsonValue:
        note_id = self.world.new_id("n")
        self.world.table("notes.items")[note_id] = {"id": note_id, "text": args.text}
        return {"id": note_id, "text": args.text}

    @tool(mutates=False, description="Get a note.")
    async def get(self, args: IdArgs) -> JsonValue:
        row = self.world.table("notes.items").get(args.id)
        if row is None:
            raise ServiceError("not_found", f"no note {args.id}")
        return row

    @tool(mutates=True, description="Edit a note.")
    async def edit(self, args: EditArgs) -> JsonValue:
        row = self.world.table("notes.items").get(args.id)
        if row is None:
            raise ServiceError("not_found", f"no note {args.id}")
        row["text"] = args.text
        return row

    @tool(mutates=True, description="Delete a note.")
    async def remove(self, args: IdArgs) -> JsonValue:
        if self.world.table("notes.items").pop(args.id, None) is None:
            raise ServiceError("not_found", f"no note {args.id}")
        return {"deleted": args.id}

    @tool(mutates=True, description="Half-writes, then crashes.")
    async def crash(self, args: AddArgs) -> JsonValue:
        self.world.table("notes.items")["half"] = {"id": "half", "text": args.text}
        raise RuntimeError("boom")

    @tool(mutates=True, description="Half-writes, then rejects.")
    async def reject(self, args: AddArgs) -> JsonValue:
        self.world.table("notes.items")["half"] = {"id": "half", "text": args.text}
        raise ServiceError("conflict", "already exists")


class JobArgs(BaseModel):
    title: str


class Scheduler(Service):
    name = "scheduler"
    tables = ("jobs",)

    @tool(mutates=True, description="Create a job.")
    async def create_job(self, args: JobArgs) -> JsonValue:
        job_id = self.world.new_id("job")
        self.world.table("scheduler.jobs")[job_id] = {"id": job_id, "title": args.title}
        return {"id": job_id}


REGISTRY: dict[str, type[Service]] = {"notes": Notes, "scheduler": Scheduler}


def make_world(rows: list[dict[str, JsonValue]] | None = None, faults: tuple[Fault, ...] = ()) -> World:
    seed = WorldSeed.model_validate(
        {
            "now": NOW,
            "user": {"name": "Alex", "email": "alex@northwind.test", "timezone": "Europe/London"},
            "services": {"notes": {"items": rows or []}},
        }
    )
    return World(seed, ["notes", "scheduler"], faults, registry=REGISTRY)


def seeded() -> list[dict[str, JsonValue]]:
    return [{"id": "n1", "text": "one"}, {"id": "n2", "text": "two"}]


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (FaultKind.RATE_LIMITED, "rate_limited"),
        (FaultKind.SERVER_ERROR, "server_error"),
        (FaultKind.AUTH_EXPIRED, "auth_expired"),
        (FaultKind.VALIDATION, "validation"),
    ],
)
async def test_error_faults_never_mutate_and_expire(kind: FaultKind, code: str) -> None:
    world = make_world(faults=(Fault(tool="notes_add", kind=kind),))
    ok, result = await world.call("notes_add", {"text": "a"})
    assert not ok
    assert isinstance(result, dict) and code in str(result["error"])
    assert world.table("notes.items") == {}
    ok2, _ = await world.call("notes_add", {"text": "a"})
    assert ok2
    first, second = world.evidence().calls
    assert (first.error, first.fault, first.mutated) == (CallError.FAULT, kind, False)
    assert (second.error, second.fault, second.mutated) == (None, None, True)


async def test_lost_response_commits_but_reports_unknown_outcome() -> None:
    world = make_world(faults=(Fault(tool="notes_add", kind=FaultKind.LOST_RESPONSE),))
    ok, result = await world.call("notes_add", {"text": "a"})
    assert not ok
    assert result == {"error": {"code": "outcome_unknown", "message": LOST_RESPONSE_MESSAGE}}
    assert list(world.table("notes.items")) == ["n1"]
    (call,) = world.evidence().calls
    assert (call.fault, call.mutated, call.error) == (FaultKind.LOST_RESPONSE, True, CallError.FAULT)


async def test_corrupt_success_claims_success_without_mutating() -> None:
    world = make_world(faults=(Fault(tool="notes_add", kind=FaultKind.CORRUPT_SUCCESS),))
    ok, result = await world.call("notes_add", {"text": "a"})
    assert ok and isinstance(result, dict)
    assert world.table("notes.items") == {}
    fake_id = result["id"]
    assert isinstance(fake_id, str)
    found, _ = await world.call("notes_get", {"id": fake_id})
    assert not found
    calls = world.evidence().calls
    assert (calls[0].fault, calls[0].mutated) == (FaultKind.CORRUPT_SUCCESS, False)
    assert calls[1].error is CallError.NOT_FOUND


async def test_fault_window_counts_calls_per_tool() -> None:
    world = make_world(faults=(Fault(tool="notes_add", on_call=2, times=0, kind=FaultKind.SERVER_ERROR),))
    outcomes = [(await world.call("notes_add", {"text": str(i)}))[0] for i in range(4)]
    assert outcomes == [True, False, False, False]


def test_fault_on_unknown_tool_is_an_eval_bug() -> None:
    with pytest.raises(WorldError, match="unknown tool"):
        make_world(faults=(Fault(tool="notes_send", kind=FaultKind.SERVER_ERROR),))


async def test_diff_reports_added_removed_and_changed_fields() -> None:
    world = make_world(seeded())
    await world.call("notes_edit", {"id": "n1", "text": "uno"})
    await world.call("notes_remove", {"id": "n2"})
    await world.call("notes_add", {"text": "three"})
    diff = world.evidence().diff.tables
    assert set(diff) == {"notes.items"}
    table = diff["notes.items"]
    assert [r["text"] for r in table.added] == ["three"]
    assert [r["id"] for r in table.removed] == ["n2"]
    (change,) = table.changed
    assert (change.id, change.fields, change.before["text"], change.after["text"]) == ("n1", ("text",), "one", "uno")


async def test_reads_do_not_count_as_mutations() -> None:
    world = make_world(seeded())
    await world.call("notes_get", {"id": "n1"})
    evidence = world.evidence()
    assert evidence.calls[0].mutated is False
    assert evidence.diff.tables == {}


async def test_service_crash_sets_internal_error_and_rolls_back() -> None:
    world = make_world(seeded())
    ok, result = await world.call("notes_crash", {"text": "x"})
    assert not ok
    assert result == {"error": {"code": "internal", "message": "Internal error."}}
    evidence = world.evidence()
    assert evidence.internal_error is not None and "RuntimeError: boom" in evidence.internal_error
    assert evidence.calls[0].error is CallError.INTERNAL
    assert evidence.diff.tables == {}


async def test_service_error_rolls_back_partial_writes() -> None:
    world = make_world(seeded())
    ok, _ = await world.call("notes_reject", {"text": "x"})
    evidence = world.evidence()
    assert not ok and evidence.calls[0].error is CallError.CONFLICT and not evidence.calls[0].mutated
    assert evidence.diff.tables == {} and evidence.internal_error is None


async def test_invalid_args_and_unknown_tools_are_validation_errors() -> None:
    world = make_world()
    assert (await world.call("notes_add", {"txt": "x"}))[0] is False
    assert (await world.call("notes_fly", {}))[0] is False
    assert [c.error for c in world.evidence().calls] == [CallError.VALIDATION, CallError.VALIDATION]


async def test_calls_are_stamped_with_position_and_clock() -> None:
    world = make_world()
    await world.call("notes_add", {"text": "a"})
    world.set_position(2, 1)
    world.advance_clock(datetime.fromisoformat("2026-06-04T10:00:00+01:00"))
    await world.call("notes_add", {"text": "b"})
    world.set_position(3, 2)
    await world.call("notes_add", {"text": "c"})
    calls = world.evidence().calls
    assert [(c.seq, c.turn, c.session) for c in calls] == [(1, 1, 1), (2, 2, 1), (3, 3, 2)]
    assert calls[1].at.day == 4


def test_clock_never_runs_backwards() -> None:
    with pytest.raises(WorldError):
        make_world().advance_clock(datetime.fromisoformat("2026-06-01T00:00:00+00:00"))


async def test_patches_are_absorbed_into_the_baseline() -> None:
    world = make_world(seeded())
    await world.call("notes_remove", {"id": "n2"})
    world.apply_patch(
        WorldPatch(
            insert={"notes.items": [{"id": "n9", "text": "arrived"}]},
            update={"notes.items": {"n2": {"text": "late edit"}}},
            rename_ids={"notes.items": {"n1": "n1b"}},
        )
    )
    evidence = world.evidence()
    assert set(evidence.final["notes.items"]) == {"n1b", "n9"}
    # Only the agent's deletion is a change; the patch's rows are part of the baseline.
    table = evidence.diff.tables["notes.items"]
    assert (table.added, [r["id"] for r in table.removed], table.changed) == ((), ["n2"], ())
    assert evidence.seed_state["notes.items"]["n2"]["text"] == "late edit"


def test_patch_on_missing_row_is_rejected() -> None:
    with pytest.raises(WorldError):
        make_world().apply_patch(WorldPatch(update={"notes.items": {"zz": {"text": "x"}}}))


async def test_new_ids_skip_seeded_ids() -> None:
    world = make_world([{"id": "n1", "text": "seeded"}])
    _, result = await world.call("notes_add", {"text": "a"})
    assert result == {"id": "n2", "text": "a"}


async def test_fire_selects_exactly_one_job_or_records_a_miss() -> None:
    world = make_world()
    await world.call("scheduler_create_job", {"title": "Weekly digest"})
    await world.call("scheduler_create_job", {"title": "Daily check"})
    await world.call("scheduler_create_job", {"title": "Daily check"})
    at = datetime.fromisoformat("2026-06-05T08:00:00+01:00")
    job = world.fire(Fire(name="fire-1", at=at, job={"title": {"contains": "weekly"}}))
    assert job is not None and job["title"] == "Weekly digest"
    assert world.fire(Fire(name="fire-2", at=at, job={"title": "daily check"})) is None
    assert world.fire(Fire(name="fire-3", at=at, job={"title": "nothing"})) is None
    assert world.now == at
    assert world.evidence().missed_fires == ("fire-2", "fire-3")


def test_seed_for_unmounted_service_is_rejected() -> None:
    seed = WorldSeed.model_validate(
        {"now": NOW, "user": {"name": "A", "email": "a@b.test", "timezone": "UTC"}, "services": {"mail": {"x": []}}}
    )
    with pytest.raises(WorldError, match="not mounted"):
        World(seed, ["notes"], [], registry=REGISTRY)


def test_truncate_marks_large_results() -> None:
    assert truncate({"a": 1}) == {"a": 1}
    big = truncate("x" * 10_000, limit=100)
    assert isinstance(big, str) and big.endswith("bytes]") and len(big) < 200
