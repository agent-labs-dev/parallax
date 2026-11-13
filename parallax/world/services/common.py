"""Helpers shared by the mock services.

Rows are plain JSON objects because that is what the world diffs and checks read. Seed
rows are authored by hand, so every accessor here fails loudly (a plain exception, which
the world records as `internal_error` and the trial becomes INVALID) instead of guessing:
a malformed seed is an eval bug, never the agent's fault.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from parallax.match import normalise
from parallax.models import JsonObject
from parallax.protocols import WorldState
from parallax.world.service import ServiceError


class Args(BaseModel):
    """Base for tool argument models: unknown arguments are a validation error, as with a real API."""

    model_config = ConfigDict(extra="forbid")


Email = Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", description="Email address")]
Limit = Annotated[int, Field(ge=1, le=100, description="Maximum number of results")]
Offset = Annotated[int, Field(ge=0, description="Skip this many results; pass the last page's next_offset")]


def page(rows: list[JsonObject], offset: int, limit: int) -> tuple[list[JsonObject], int, int | None]:
    """One page of `rows`, the number of rows in all, and the offset of the next page (None on the last one)."""
    chunk = rows[offset : offset + limit]
    end = offset + len(chunk)
    return chunk, len(rows), end if end < len(rows) else None


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def now_iso(world: WorldState) -> str:
    return iso(world.now)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ServiceError("validation", f"unknown time zone {name!r}") from exc


def localize(dt: datetime, tz: str) -> datetime:
    """Naive times are read in `tz` (what a calendar UI does); aware ones are converted to it."""
    z = zone(tz)
    return dt.replace(tzinfo=z) if dt.tzinfo is None else dt.astimezone(z)


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"seed datetime {value!r} has no offset")
    return dt


def json_list(items: Iterable[JsonValue]) -> list[JsonValue]:
    return list(items)


def get_str(row: JsonObject, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise TypeError(f"row {row.get('id')!r}: field {key!r} must be a string, got {value!r}")
    return value


def opt_str(row: JsonObject, key: str) -> str | None:
    value = row.get(key)
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"row {row.get('id')!r}: field {key!r} must be a string or null, got {value!r}")


def get_num(row: JsonObject, key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"row {row.get('id')!r}: field {key!r} must be a number, got {value!r}")
    return float(value)


def str_list(row: JsonObject, key: str) -> list[str]:
    """A list-of-strings field; absent means empty (seeds routinely omit empty cc/labels)."""
    value = row.get(key, [])
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TypeError(f"row {row.get('id')!r}: field {key!r} must be a list of strings, got {value!r}")
    return [v for v in value if isinstance(v, str)]


def require(world: WorldState, table: str, row_id: str, noun: str) -> JsonObject:
    row = world.table(table).get(row_id)
    if row is None:
        raise ServiceError("not_found", f"no {noun} with id {row_id!r}")
    return row


def resolve(world: WorldState, table: str, ref: str, noun: str, *, keys: Sequence[str] = ("name",)) -> JsonObject:
    """Look a row up by id, then by an exact (normalised) match on one of `keys`; ambiguity is a conflict."""
    rows = world.table(table)
    if ref in rows:
        return rows[ref]
    wanted = normalise(ref.lstrip("#"))
    hits = [r for r in rows.values() if any(normalise(str(r.get(k, "")).lstrip("#")) == wanted for k in keys)]
    if not hits:
        raise ServiceError("not_found", f"no {noun} matching {ref!r}")
    if len(hits) > 1:
        ids = ", ".join(str(h.get("id")) for h in hits)
        raise ServiceError("conflict", f"{ref!r} matches several {noun}s ({ids}); pass an id")
    return hits[0]


def text_hit(row: JsonObject, fields: Sequence[str], query: str | None) -> bool:
    """Every query word appears in one of `fields` (case/diacritic-insensitive)."""
    if not query:
        return True
    haystack = normalise(" ".join(_flat(row.get(f)) for f in fields))
    return all(word in haystack for word in normalise(query).split())


def _flat(value: JsonValue) -> str:
    if isinstance(value, list):
        return " ".join(_flat(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_flat(v) for v in value.values())
    return "" if value is None else str(value)


SNIPPET_SIZE = 160


def snippet(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= SNIPPET_SIZE else flat[: SNIPPET_SIZE - 1] + "…"


def updated(row: JsonObject, changes: dict[str, JsonValue]) -> JsonObject:
    """A copy of `row` with the non-null `changes` applied (null means 'leave unchanged')."""
    return {**row, **{k: v for k, v in changes.items() if v is not None}}
