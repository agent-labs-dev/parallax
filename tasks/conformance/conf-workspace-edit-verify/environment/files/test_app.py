import pytest
from app import apply_discount


def test_ten_percent_off():
    assert apply_discount(100, 10) == pytest.approx(90)


def test_quarter_off():
    assert apply_discount(80, 25) == pytest.approx(60)


def test_no_discount():
    assert apply_discount(50, 0) == pytest.approx(50)
