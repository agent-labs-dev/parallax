from payouts.form import errors


def test_valid_form():
    assert errors({"payee": "A Tenant", "sort_code": "12-34-56", "account": "12345678"}) == []


def test_bad_sort_code():
    assert errors({"payee": "A Tenant", "sort_code": "12-34", "account": "12345678"}) == ["sort_code"]
