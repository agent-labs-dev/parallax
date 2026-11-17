from __future__ import annotations

from parallax.harnesses.browser.clock import SESSION_INCLUDED, TIMING_BASIS, AttemptClock


def test_clock_breaks_open_agent_and_close_into_one_basis() -> None:
    clock = AttemptClock(bounded=False)
    clock.ready(10.0)
    clock.asked(12.0)
    clock.page_ready(15.0)
    clock.answered(20.0)
    clock.closed_at(23.0)

    timing = clock.timing()
    assert timing.basis == TIMING_BASIS
    assert timing.breakdown == {"open": 3000.0, "agent": 7000.0, "close": 3000.0}


def test_unready_clock_identifies_session_included_basis() -> None:
    assert AttemptClock(bounded=False).timing().basis == SESSION_INCLUDED
