"""Value matching shared by checks, user rules and `World.find_job`.

Stdlib only. Every check funnels through here, so a silent mis-parse would corrupt
every verdict: unparseable *authored* bounds raise, unparseable *observed* values
simply do not match.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta
from enum import Enum
from typing import TypeGuard
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from parallax.models import JsonObject, Match, Matcher, Scalar


class Missing(Enum):
    """A field that is not present at all (distinct from JSON null)."""

    MISSING = "missing"


MISSING = Missing.MISSING
Value = JsonValue | Missing

_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
                         "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"'})  # fmt: skip


def normalise(text: str) -> str:
    """NFKC, straighten quotes, casefold, collapse whitespace."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(_QUOTES).casefold().split())


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------

# A number must not be glued to a preceding word character or decimal point, so
# "v1.2", "Q3" and "abc123" yield nothing. A sign counts only when it is not glued
# either, so the dashes in "2026-06-03" are separators, not minus signs.
_NUMBER = re.compile(
    r"""
    (?<![\w.])
    (?P<sign>[-−+](?=[$£€¥]?\d|[$£€¥]?\.\d))?
    (?P<cur>[$£€¥])?
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)
    (?P<suf>\s?(?:k|K|thousand|M|mn|million|bn|billion)\b|\s?%)?
    """,
    re.VERBOSE,
)
# Lowercase "m" is deliberately not a multiplier: "5m" is minutes/metres far more often than millions.
_MULTIPLIER = {
    "k": 1e3,
    "K": 1e3,
    "thousand": 1e3,
    "M": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "bn": 1e9,
    "billion": 1e9,
    "%": 1.0,  # "12%" is the number 12: authors write `number: 12`
}


def _value(m: re.Match[str]) -> float:
    value = float(m["num"].replace(",", ""))
    suffix = (m["suf"] or "").strip()
    if suffix:
        value *= _MULTIPLIER[suffix]
    return -value if m["sign"] in ("-", "−") else value


def numbers_in(text: str) -> tuple[float, ...]:
    """Every number mentioned in free text, in order."""
    return tuple(_value(m) for m in _NUMBER.finditer(unicodedata.normalize("NFKC", text)))


def parse_number(text: str) -> float | None:
    """The number when the whole text is one numeric token ("$1,234.50", "£45", "1.2k", "12%")."""
    m = _NUMBER.fullmatch(unicodedata.normalize("NFKC", text).strip())
    return _value(m) if m else None


# ---------------------------------------------------------------------------
# datetimes
# ---------------------------------------------------------------------------

_ORDINAL = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.IGNORECASE)
_WITH_YEAR = ("%d %B %Y", "%B %d %Y", "%d %b %Y", "%b %d %Y")
_NO_YEAR = ("%d %B", "%d %b", "%B %d", "%b %d")
# English names regardless of process locale, full and three-letter ("Tuesday", "tue").
_WEEKDAYS = {
    name: i
    for i, full in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))
    for name in (full, full[:3])
}


def parse_when(text: str, tz: str, now: datetime | None = None) -> datetime | None:
    """An aware datetime from ISO or a few human formats, read in the persona timezone `tz`.

    Year-less dates take `now`'s year; "Tuesday 14:00" is the first such weekday on or
    after `now`'s date. Both need `now`, and yield None without it rather than guessing.
    """
    zone = ZoneInfo(tz)
    raw = unicodedata.normalize("NFKC", text).strip()
    try:
        iso = datetime.fromisoformat(raw)
    except ValueError:
        pass
    else:
        return iso if iso.tzinfo is not None else iso.replace(tzinfo=zone)
    cleaned = " ".join(_ORDINAL.sub("", raw).replace(",", " ").split())
    for fmt in _WITH_YEAR:
        parsed = _strptime(cleaned, fmt)
        if parsed is not None:
            return parsed.replace(tzinfo=zone)
    if now is None:
        return None
    local_now = now.astimezone(zone)
    for fmt in _NO_YEAR:
        parsed = _strptime(f"{cleaned} {local_now.year}", f"{fmt} %Y")  # year given so Feb 29 parses
        if parsed is not None:
            return parsed.replace(tzinfo=zone)
    return _weekday_time(cleaned, local_now, zone)


def _weekday_time(text: str, local_now: datetime, zone: ZoneInfo) -> datetime | None:
    # strptime accepts %A but silently ignores it without a date, so the weekday is looked up by hand.
    name, _, clock = text.partition(" ")
    weekday = _WEEKDAYS.get(name.casefold())
    parsed = _strptime(clock, "%H:%M")
    if weekday is None or parsed is None:
        return None
    day = local_now.date() + timedelta(days=(weekday - local_now.weekday()) % 7)
    return datetime.combine(day, parsed.time(), tzinfo=zone)


def _strptime(text: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(text, fmt)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def _is_number(v: object) -> TypeGuard[int | float]:
    return isinstance(v, int | float) and not isinstance(v, bool)


def get_path(obj: JsonObject, path: str) -> Value:
    """Resolve a dotted key ("scope.charge"); a literal key containing dots wins over nesting."""
    if path in obj:
        return obj[path]
    head, dot, rest = path.partition(".")
    if not dot or head not in obj:
        return MISSING
    inner = obj[head]
    return get_path(inner, rest) if isinstance(inner, dict) else MISSING


def scalar_eq(value: Value, want: Scalar) -> bool:
    if value is MISSING:
        return want is None
    if want is None or value is None:
        return value is want
    if isinstance(want, bool) or isinstance(value, bool):
        if isinstance(want, bool) and isinstance(value, bool):
            return value is want
        other = value if isinstance(want, bool) else want
        flag = want if isinstance(want, bool) else value
        return isinstance(other, str) and normalise(other) == ("true" if flag else "false")
    if isinstance(want, str):
        if isinstance(value, str):
            return normalise(value) == normalise(want)
        return _is_number(value) and parse_number(want) == value  # lists and objects never equal a scalar
    if _is_number(value):
        return value == want
    return isinstance(value, str) and parse_number(value) == want


def match(value: Value, matcher: Matcher, tz: str, now: datetime | None = None) -> bool:
    """Whether `value` satisfies every condition set on `matcher` (a bare scalar means eq)."""
    if matcher is None:  # bare null: Match(eq=None) would be an empty Match, so handle it here
        return _absent(value)
    m = matcher if isinstance(matcher, Match) else Match(eq=matcher)
    conditions = (
        (m.absent is None or _absent(value) is m.absent),
        (m.eq is None or scalar_eq(value, m.eq)),
        (m.ne is None or not scalar_eq(value, m.ne)),
        (m.contains is None or _contains(value, m.contains)),
        (m.any_of is None or any(scalar_eq(value, a) for a in m.any_of)),
        (m.none_of is None or not any(scalar_eq(value, a) for a in m.none_of)),
        (m.regex is None or _regex(value, m.regex)),
        (m.number is None or _number(value, m.number, m.tol)),
        (m.gte is None or _compare(value, m.gte, tz, now, lower=True)),
        (m.lte is None or _compare(value, m.lte, tz, now, lower=False)),
    )
    return all(conditions)


def match_fields(row: JsonObject, where: dict[str, Matcher], tz: str, now: datetime | None = None) -> bool:
    """Every dotted key in `where` resolves in `row` and matches."""
    return all(match(get_path(row, key), m, tz, now) for key, m in where.items())


def _absent(value: Value) -> bool:
    return value is MISSING or value is None


def _contains(value: Value, needles: str | tuple[str, ...]) -> bool:
    wanted = (needles,) if isinstance(needles, str) else needles
    if isinstance(value, list):
        return all(any(scalar_eq(item, n) for item in value) for n in wanted)
    if isinstance(value, str):
        text = normalise(value)
        return all(normalise(n) in text for n in wanted)
    return False


def _regex(value: Value, pattern: str) -> bool:
    # An object or list is searched as its JSON text, so a pattern can pin a value whatever key an
    # agent filed it under ({"charge": "ch_A"} and {"charge_to_refund": "ch_A"} alike).
    if isinstance(value, bool) or value is MISSING or value is None:
        return False
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, dict | list) else str(value)
    return re.search(pattern, text, re.IGNORECASE) is not None


def _number(value: Value, want: float, tol: float) -> bool:
    # The epsilon absorbs binary float error ("0.3" vs 0.1 + 0.2) without loosening authored tolerances.
    limit = tol + 1e-9 * max(1.0, abs(want))
    if _is_number(value):
        return abs(value - want) <= limit
    if isinstance(value, str):
        return any(abs(n - want) <= limit for n in numbers_in(value))
    return False


def _compare(value: Value, bound: float | str, tz: str, now: datetime | None, *, lower: bool) -> bool:
    if isinstance(bound, str):
        limit = parse_when(bound, tz, now)
        if limit is None:
            raise ValueError(f"unparseable datetime bound {bound!r}")
        observed = parse_when(value, tz, now) if isinstance(value, str) else None
        if observed is None:
            return False
        return observed >= limit if lower else observed <= limit
    number = value if _is_number(value) else parse_number(value) if isinstance(value, str) else None
    if number is None:
        return False
    return number >= bound if lower else number <= bound
