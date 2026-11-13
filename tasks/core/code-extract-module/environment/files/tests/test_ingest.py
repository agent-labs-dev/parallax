from datetime import date
from decimal import Decimal

from ingest.loader import load_line
from ingest.summary import net_total


def test_load_line():
    line = load_line("03/06/2026| Corner Shop |1,204.10")
    assert line.when == date(2026, 6, 3)
    assert line.payee == "Corner Shop"
    assert line.amount == Decimal("1204.10")


def test_credit_is_negative():
    assert load_line("12/06/2026|Refund|15.00 CR").amount == Decimal("-15.00")


def test_net_total():
    assert net_total(["10.00", "2.50 CR", "1,000.00"]) == Decimal("1007.50")
