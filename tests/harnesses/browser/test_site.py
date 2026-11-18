from __future__ import annotations

import pytest
from parallax.harnesses.browser.site import (
    RETRYABLE_STATUS,
    Unavailable,
    refusal,
    retryable_status,
)


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
def test_retryable_statuses_are_explicit_and_refusals_are_not(status: int) -> None:
    assert retryable_status(status)
    assert not refusal(status)
    assert refusal(401)


def test_provider_outage_exception_has_a_stable_type() -> None:
    assert issubclass(Unavailable, RuntimeError)
