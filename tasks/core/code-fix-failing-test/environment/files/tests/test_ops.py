from pricing.ops import add_tax, apply_discount


def test_discount_takes_percentage_off():
    assert apply_discount(200, 10) == 180


def test_zero_discount_is_identity():
    assert apply_discount(50, 0) == 50


def test_tax_is_added():
    assert add_tax(100, 20) == 120
