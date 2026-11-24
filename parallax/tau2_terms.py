"""The tau2 termination-reason contract, shared by the model and the bridge.

Stdlib-only and importable by file path, so `tau2_bridge.py` (run by the pinned tau2
checkout's own Python, without parallax installed) loads this same source instead of
copying the list.
"""

from __future__ import annotations

from typing import Literal, get_args

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
