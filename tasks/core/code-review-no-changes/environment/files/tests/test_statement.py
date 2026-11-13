from datetime import date
from decimal import Decimal

from invoicing.statement import statement_line


def test_invoice_not_yet_due_has_no_fee():
    line = statement_line("INV-7", Decimal("100.00"), due=date(2026, 7, 1), today=date(2026, 6, 3))
    assert line == "INV-7: 100.00"
