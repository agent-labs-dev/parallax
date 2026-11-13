from pricing.basket import basket_total


def test_basket_without_discount():
    assert basket_total([(10.0, 2), (5.0, 1)]) == 30.0


def test_basket_with_discount():
    assert basket_total([(100.0, 1)], discount_pct=25) == 90.0
