"""The tau2 termination-reason contract, shared by the model and the bridge.

Stdlib-only and importable by file path, so `tau2_bridge.py` (run by the pinned tau2
checkout's own Python, without parallax installed) loads this same source instead of
copying the list.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, NamedTuple, get_args

TerminationReason = Literal[
    "user_stop",
    "agent_stop",
    "max_steps",
    "timeout",
    "too_many_errors",
    "agent_error",
    "user_error",
    "infrastructure_error",
    "context_window_exceeded",
    "unexpected_error",
]

TERMINATION_REASONS: tuple[str, ...] = get_args(TerminationReason)

Tau2Actor = Literal["user", "agent", "env"]


class Tau2Event(NamedTuple):
    """One upstream message or environment batch in a recorded tau2 sequence."""

    actor: Tau2Actor
    waits_for_environment: bool = False
    stop: bool = False
    error: bool = False
    error_count: int = 0


class Tau2Progress(NamedTuple):
    step_count: int
    num_errors: int
    termination: str | None
    cut: int


def tau2_progress(events: Sequence[Tau2Event], max_steps: int, max_errors: int) -> Tau2Progress:
    """Mirror upstream ``step`` and ``_check_termination`` over recorded events.

    A message that sends tool calls waits for its environment batch, so limits are not checked
    until that batch event. Max steps is checked before max errors, and max errors wins when both
    limits become true in the same check.
    """
    steps = 0
    errors = 0
    termination: str | None = None
    for cut, event in enumerate(events, start=1):
        steps += 1
        errors += event.error_count if event.error_count else int(event.error)
        if event.stop:
            termination = "user_stop" if event.actor == "user" else "agent_stop"
        if event.waits_for_environment:
            continue
        if steps >= max_steps:
            termination = "max_steps"
        if errors >= max_errors:
            termination = "too_many_errors"
        if termination is not None and termination in {"max_steps", "too_many_errors", "user_stop", "agent_stop"}:
            return Tau2Progress(steps, errors, termination, cut)
    return Tau2Progress(steps, errors, termination, len(events))
