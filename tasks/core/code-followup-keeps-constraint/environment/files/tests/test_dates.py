from datetime import date

import pytest
from feedsync.dates import DateError, parse_feed_date
from feedsync.rows import accept


def test_iso():
    assert parse_feed_date("2026-06-03") == date(2026, 6, 3)


def test_garbage_is_rejected():
    with pytest.raises(DateError):
        parse_feed_date("next tuesday")


def test_accept_splits_rows():
    good, rejected = accept([{"date": "2026-06-03"}, {"date": "soon"}])
    assert [r["date"] for r in good] == [date(2026, 6, 3)]
    assert rejected == ["soon"]
