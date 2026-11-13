from ledger.report import total_by_account
from ledger.source import fetch_rows
from ledger.sync import preview


def test_limit_caps_rows(exports):
    assert [r["account"] for r in fetch_rows(exports, "entries", limit=2)] == ["rent", "food"]


def test_totals(exports):
    assert total_by_account(exports) == {"rent": 125000, "food": 4550}


def test_preview(exports):
    assert preview(exports) == ["rent: £1,200.00", "food: £45.50"]
