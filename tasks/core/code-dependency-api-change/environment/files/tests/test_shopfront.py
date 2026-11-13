from shopfront.export import csv_amount
from shopfront.receipt import discount_note, receipt_line


def test_receipt_line():
    assert receipt_line("Tea", 250, 3) == "3 x Tea @ $2.50 = $7.50"


def test_large_totals_are_grouped():
    assert receipt_line("Desk", 125_000, 2) == "2 x Desk @ $1,250.00 = $2,500.00"


def test_discount_note():
    assert discount_note(0.125) == "Saved 12.5%"


def test_csv_amount_uses_currency_symbol():
    assert csv_amount(1999, "GBP") == "£19.99"
