"""Sheets: simple spreadsheets with named columns.

Tables:
- sheets: {id, title, columns[]}
- rows:   {id, sheet, position, <column>: value, ...}; cells sit directly on the row so a check can
  address them by column name. `id`, `sheet` and `position` are therefore reserved column names.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field, JsonValue

from parallax.models import JsonObject, Scalar
from parallax.world.service import Service, ServiceError, tool
from parallax.world.services.common import Args, get_num, get_str, json_list, require, resolve, str_list, text_hit

RESERVED = frozenset({"id", "sheet", "position"})

SheetRef = Field(description="Sheet id or title")


class ReadArgs(Args):
    sheet: str = SheetRef
    query: str | None = Field(None, description="Only rows where every word appears in some cell")


class AppendArgs(Args):
    sheet: str = SheetRef
    values: dict[str, Scalar] = Field(description="Column name -> value; missing columns are left empty")


class CellArgs(Args):
    row_id: str
    column: str
    value: Scalar


class RowIdArgs(Args):
    row_id: str


class UpdateRowArgs(Args):
    row_id: str
    values: dict[str, Scalar] = Field(description="Column name -> new value; other cells are kept")


class CreateSheetArgs(Args):
    title: str = Field(min_length=1)
    columns: list[str] = Field(min_length=1)


class SheetsService(Service):
    name: ClassVar[str] = "sheets"
    tables: ClassVar[tuple[str, ...]] = ("sheets", "rows")

    @tool(mutates=False, description="List spreadsheets and their columns.")
    async def list_sheets(self, args: Args) -> JsonValue:
        return {"sheets": json_list(self.world.table("sheets.sheets").values())}

    @tool(mutates=False, description="Read the rows of a spreadsheet in order.")
    async def read(self, args: ReadArgs) -> JsonValue:
        sheet = self._sheet(args.sheet)
        rows = [r for r in self._rows(get_str(sheet, "id")) if text_hit(r, str_list(sheet, "columns"), args.query)]
        return {"sheet": sheet, "rows": json_list(rows)}

    @tool(mutates=True, additive=True, description="Append a row to the end of a spreadsheet.")
    async def append_row(self, args: AppendArgs) -> JsonValue:
        sheet = self._sheet(args.sheet)
        sheet_id = get_str(sheet, "id")
        _check_columns(sheet, args.values)
        existing = self._rows(sheet_id)
        position = int(get_num(existing[-1], "position")) + 1 if existing else 1
        row: JsonObject = {"id": self.world.new_id("row_"), "sheet": sheet_id, "position": position}
        row |= {col: args.values.get(col) for col in str_list(sheet, "columns")}
        self.world.table("sheets.rows")[get_str(row, "id")] = row
        return row

    @tool(mutates=True, description="Set one cell.")
    async def update_cell(self, args: CellArgs) -> JsonValue:
        return self._update(args.row_id, {args.column: args.value})

    @tool(mutates=True, description="Set several cells of one row.")
    async def update_row(self, args: UpdateRowArgs) -> JsonValue:
        return self._update(args.row_id, args.values)

    @tool(mutates=True, description="Delete a row.")
    async def delete_row(self, args: RowIdArgs) -> JsonValue:
        require(self.world, "sheets.rows", args.row_id, "row")
        self.world.table("sheets.rows").pop(args.row_id)
        return {"deleted": args.row_id}

    @tool(mutates=True, additive=True, description="Create an empty spreadsheet with the given columns.")
    async def create_sheet(self, args: CreateSheetArgs) -> JsonValue:
        bad = sorted(RESERVED & set(args.columns))
        if bad or len(set(args.columns)) != len(args.columns):
            raise ServiceError("validation", f"columns: must be unique and not {', '.join(sorted(RESERVED))}")
        row: JsonObject = {"id": self.world.new_id("sheet_"), "title": args.title, "columns": json_list(args.columns)}
        self.world.table("sheets.sheets")[get_str(row, "id")] = row
        return row

    def _sheet(self, ref: str) -> JsonObject:
        return resolve(self.world, "sheets.sheets", ref, "sheet", keys=("title",))

    def _rows(self, sheet_id: str) -> list[JsonObject]:
        rows = [r for r in self.world.table("sheets.rows").values() if r.get("sheet") == sheet_id]
        return sorted(rows, key=lambda r: get_num(r, "position"))

    def _update(self, row_id: str, values: dict[str, Scalar]) -> JsonObject:
        row = require(self.world, "sheets.rows", row_id, "row")
        sheet = require(self.world, "sheets.sheets", get_str(row, "sheet"), "sheet")
        _check_columns(sheet, values)
        new: JsonObject = {**row, **values}
        self.world.table("sheets.rows")[row_id] = new
        return new


def _check_columns(sheet: JsonObject, values: dict[str, Scalar]) -> None:
    columns = str_list(sheet, "columns")
    unknown = sorted(set(values) - set(columns))
    if unknown:
        raise ServiceError("validation", f"unknown columns {unknown}; the sheet has {columns}")
