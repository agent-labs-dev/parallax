"""Tables: a record database (CRM-style) of bases with named fields.

Tables:
- bases:   {id, name, fields[]}
- records: {id, base, created, <field>: value, ...}; values sit directly on the record so a check can
  address them by field name. `id`, `base` and `created` are therefore reserved field names.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, JsonValue

from parallax.match import normalise
from parallax.models import JsonObject, Scalar
from parallax.world.service import Service, ServiceError, tool
from parallax.world.services.common import (
    Args,
    Limit,
    get_str,
    json_list,
    now_iso,
    require,
    resolve,
    str_list,
    text_hit,
)

BaseRef = Field(description="Base id or name")


class ListArgs(Args):
    base: str = BaseRef
    where: dict[str, Scalar] = Field({}, description="Field -> value; only records whose fields equal these")
    query: str | None = Field(None, description="Words that must appear in some field")
    limit: Limit = 100
    offset: int = Field(0, ge=0, description="Skip this many matching records; pass the last page's next_offset")


class RecordIdArgs(Args):
    id: str = Field(description="Record id")


class CreateArgs(Args):
    base: str = BaseRef
    fields: dict[str, Scalar] = Field(description="Field name -> value")


class UpdateArgs(Args):
    id: str = Field(description="Record id")
    fields: dict[str, Scalar] = Field(min_length=1, description="Field name -> new value; others are kept")


class TablesService(Service):
    name: ClassVar[str] = "tables"
    tables: ClassVar[tuple[str, ...]] = ("bases", "records")

    @tool(mutates=False, description="List the record bases and their fields.")
    async def list_bases(self, args: Args) -> JsonValue:
        return {"bases": json_list(self.world.table("tables.bases").values())}

    @tool(
        mutates=False,
        description="List records in a base, optionally filtered by exact field values, one page at a time.",
    )
    async def list_records(self, args: ListArgs) -> JsonValue:
        base = self._base(args.base)
        _check_fields(base, args.where)
        rows = [
            r
            for r in self.world.table("tables.records").values()
            if r.get("base") == base["id"]
            and all(_same(r.get(k), v) for k, v in args.where.items())
            and text_hit(r, str_list(base, "fields"), args.query)
        ]
        page = rows[args.offset : args.offset + args.limit]
        end = args.offset + len(page)
        return {"records": json_list(page), "total": len(rows), "next_offset": end if end < len(rows) else None}

    @tool(mutates=False, description="Get one record.")
    async def get_record(self, args: RecordIdArgs) -> JsonValue:
        return require(self.world, "tables.records", args.id, "record")

    @tool(mutates=True, additive=True, description="Create a record in a base.")
    async def create_record(self, args: CreateArgs) -> JsonValue:
        base = self._base(args.base)
        _check_fields(base, args.fields)
        row: JsonObject = {"id": self.world.new_id("rec_"), "base": base["id"], "created": now_iso(self.world)}
        row |= {f: args.fields.get(f) for f in str_list(base, "fields")}
        self.world.table("tables.records")[get_str(row, "id")] = row
        return row

    @tool(mutates=True, description="Update fields of a record.")
    async def update_record(self, args: UpdateArgs) -> JsonValue:
        row = require(self.world, "tables.records", args.id, "record")
        _check_fields(require(self.world, "tables.bases", get_str(row, "base"), "base"), args.fields)
        new: JsonObject = {**row, **args.fields}
        self.world.table("tables.records")[args.id] = new
        return new

    @tool(mutates=True, description="Delete a record.")
    async def delete_record(self, args: RecordIdArgs) -> JsonValue:
        require(self.world, "tables.records", args.id, "record")
        self.world.table("tables.records").pop(args.id)
        return {"deleted": args.id}

    def _base(self, ref: str) -> JsonObject:
        return resolve(self.world, "tables.bases", ref, "base")


def _same(value: JsonValue, want: Scalar) -> bool:
    if isinstance(value, str) and isinstance(want, str):
        return normalise(value) == normalise(want)
    return value == want


def _check_fields(base: JsonObject, values: dict[str, Scalar]) -> None:
    fields = str_list(base, "fields")
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise ServiceError("validation", f"unknown fields {unknown}; the base has {fields}")
