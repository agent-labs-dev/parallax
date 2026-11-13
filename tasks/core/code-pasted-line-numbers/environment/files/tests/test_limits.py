from budget.limits import needs_warning, remaining


def test_remaining_under_budget():
    assert remaining(20_000) == 130_000


def test_warning_threshold():
    assert needs_warning(120_000)
    assert not needs_warning(119_999)
