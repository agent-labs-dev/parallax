"""Work a run does first when it is interrupted, before its tasks unwind.

A trial's own cleanup runs as its task unwinds, behind slower steps such as a worker's exit, and a CI runner that
cancels a job kills the process within seconds. What must not wait for that (stopping a paid cloud browser)
registers here, and `runner.run_to_teardown` starts it on the first SIGINT or SIGTERM.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

type Hook = Callable[[], Coroutine[Any, Any, None]]

_hooks: list[Hook] = []


def on_interrupt(hook: Hook) -> None:
    """Run `hook` when the run is interrupted. Registering the same hook again adds nothing."""
    if hook not in _hooks:
        _hooks.append(hook)


async def interrupted() -> None:
    """Every hook, concurrently. A failing hook does not stop the others."""
    await asyncio.gather(*(hook() for hook in _hooks), return_exceptions=True)
