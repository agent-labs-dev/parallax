"""Number and date parsing edge cases: the one place a silent bug corrupts every check."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from parallax.match import MISSING, get_path, match, match_fields, normalise, numbers_in, parse_number, parse_when
from parallax.models import Match

LONDON = "Europe/London"
NOW = datetime(2026, 6, 3, 9, 12, tzinfo=ZoneInfo(LONDON))  # a Wednesday


def test_normalise_folds_case_quotes_width_and_whitespace() -> None:
    assert normalise("  Don’t\n  “SEND”  Ａ ") == 'don\'t "send" a'


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("$1,234.50", 1234.5),
        ("£45", 45.0),
        ("1.2k", 1200.0),
        ("12%", 12.0),
        ("-3", -3.0),
        ("−$5", -5.0),
        ("2.5 million", 2_500_000.0),
        ("3bn", 3e9),
        (".5", 0.5),
        ("1,234,567", 1234567.0),
    ],
)
def test_parse_number_accepts_one_numeric_token(text: str, want: float) -> None:
    assert parse_number(text) == pytest.approx(want)


@pytest.mark.parametrize("text", ["", "abc", "v1.2", "12 apples", "1,23", "5m"])
def test_parse_number_rejects_anything_but_one_token(text: str) -> None:
    assert parse_number(text) is None


def test_numbers_in_ignores_glued_tokens_and_date_dashes() -> None:
    assert numbers_in("Q3 total was $1,234.50 (v1.2), up 12% on 2026-06-03") == (1234.5, 12.0, 2026.0, 6.0, 3.0)


def test_numbers_in_keeps_list_items_and_skips_non_multiplier_suffixes() -> None:
    assert numbers_in("1,2,3 and 20 kids in 5m") == (1.0, 2.0, 3.0, 20.0, 5.0)


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("2026-06-04T15:00:00+00:00", datetime(2026, 6, 4, 16, 0, tzinfo=ZoneInfo(LONDON))),
        ("2026-06-04T15:00:00Z", datetime(2026, 6, 4, 16, 0, tzinfo=ZoneInfo(LONDON))),
        ("2026-06-04 15:00", datetime(2026, 6, 4, 15, 0, tzinfo=ZoneInfo(LONDON))),
        ("2026-06-04", datetime(2026, 6, 4, tzinfo=ZoneInfo(LONDON))),
        ("4 June 2026", datetime(2026, 6, 4, tzinfo=ZoneInfo(LONDON))),
        ("June 4th, 2026", datetime(2026, 6, 4, tzinfo=ZoneInfo(LONDON))),
        ("Jun 4, 2026", datetime(2026, 6, 4, tzinfo=ZoneInfo(LONDON))),
    ],
)
def test_parse_when_absolute_forms(text: str, want: datetime) -> None:
    got = parse_when(text, LONDON)
    assert got == want
    assert got is not None and got.tzinfo is not None


def test_parse_when_naive_iso_is_read_in_persona_timezone_across_dst() -> None:
    winter = parse_when("2026-01-10T09:00", LONDON)
    summer = parse_when("2026-07-10T09:00", LONDON)
    assert winter is not None and summer is not None
    assert winter.utcoffset() != summer.utcoffset()


def test_parse_when_relative_forms_need_now() -> None:
    assert parse_when("4 Jun", LONDON) is None
    assert parse_when("4 Jun", LONDON, NOW) == datetime(2026, 6, 4, tzinfo=ZoneInfo(LONDON))
    # Same weekday as NOW resolves to today, earlier weekdays roll to next week.
    assert parse_when("Wednesday 14:00", LONDON, NOW) == datetime(2026, 6, 3, 14, 0, tzinfo=ZoneInfo(LONDON))
    assert parse_when("Monday 09:30", LONDON, NOW) == datetime(2026, 6, 8, 9, 30, tzinfo=ZoneInfo(LONDON))


def test_parse_when_rejects_garbage_and_impossible_dates() -> None:
    assert parse_when("next week sometime", LONDON, NOW) is None
    assert parse_when("31 February 2026", LONDON, NOW) is None


def test_bare_scalar_matchers_compare_after_normalisation() -> None:
    assert match("  PRIYA@Northwind.test ", "priya@northwind.test", LONDON)
    assert match("45", 45, LONDON)
    assert match(45, "45", LONDON)
    assert not match(1, True, LONDON)  # bool is never a number
    assert match("True", True, LONDON)
    assert match(None, None, LONDON) and match(MISSING, None, LONDON)
    assert not match(["a"], "a", LONDON)


def test_contains_is_substring_on_text_and_membership_on_lists() -> None:
    assert match("Re: Q3 vendor shortlist", Match(contains="vendor SHORTLIST"), LONDON)
    assert match(["alex@x.test", "sam@x.test"], Match(contains=("SAM@x.test", "alex@x.test")), LONDON)
    assert not match(["samuel@x.test"], Match(contains="sam@x.test"), LONDON)


def test_number_matcher_finds_any_number_in_text_within_tolerance() -> None:
    assert match("The fresh total is £475.00.", Match(number=475), LONDON)
    assert match("about 1.2k", Match(number=1190, tol=10), LONDON)
    assert not match("total 476", Match(number=475), LONDON)
    assert match(0.1 + 0.2, Match(number=0.3), LONDON)


def test_datetime_bounds_compare_instants_across_offsets() -> None:
    window = Match(gte="2026-06-04T09:00:00+01:00", lte="2026-06-04T10:00:00+01:00")
    assert match("2026-06-04T08:30:00Z", window, LONDON)
    assert not match("2026-06-04T09:30:00Z", window, LONDON)
    assert not match("not a date", window, LONDON)
    with pytest.raises(ValueError, match="unparseable"):
        match("2026-06-04", Match(gte="someday"), LONDON)


def test_absent_distinguishes_missing_and_null_from_present() -> None:
    row = {"id": "m1", "scope": {"charge": "ch_B"}, "cc": None}
    assert get_path(row, "scope.charge") == "ch_B"
    assert get_path(row, "scope.amount") is MISSING
    assert match_fields(row, {"cc": Match(absent=True), "bcc": Match(absent=True), "id": Match(absent=False)}, LONDON)
    assert match_fields(row, {"scope.charge": Match(any_of=("ch_a", "ch_b")), "id": Match(ne="m2")}, LONDON)


def test_regex_searches_objects_and_lists_as_json_text() -> None:
    exact_ch_a = Match(regex='"ch_A"')
    assert match({"charge_to_refund": "ch_A", "amount": 450}, exact_ch_a, LONDON)
    assert match({"charges": ["ch_A"]}, exact_ch_a, LONDON)
    assert not match({"charge": "ch_B", "reason": "keeping ch_A"}, exact_ch_a, LONDON)
    assert not match(True, Match(regex="true"), LONDON) and not match(MISSING, Match(regex=".*"), LONDON)


def test_empty_match_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one condition"):
        Match(tol=1.0)
