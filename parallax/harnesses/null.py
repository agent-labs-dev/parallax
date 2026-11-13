"""Null controls: a harness that does nothing and either claims success or asks.

`null-claim` is the false-success control: a task it passes grades the answer, not the work.
`null-refuse` is the inaction control: a restraint task it passes rewards doing nothing.
"""

from __future__ import annotations

from typing import Literal, assert_never

from parallax import __version__
from parallax.models import (
    Capability,
    CostSource,
    HarnessInfo,
    HarnessKind,
    Isolation,
    Trial,
    TurnInput,
    TurnOutput,
    TurnStatus,
)
from parallax.protocols import BoxHandle

NullMode = Literal["claim", "refuse"]
CLAIM_ANSWER = "Done, all handled."
REFUSE_ANSWER = "I can't do that from here - what would you like me to do?"


def null_info(mode: NullMode) -> HarnessInfo:
    return HarnessInfo(
        name=f"null-{mode}",
        kind=HarnessKind.CONTROL,
        # Every capability, so no task is ever NA for a null: the control must run everywhere.
        capabilities=frozenset(Capability),
        pins_model=False,
        cost_source=CostSource.NONE,
        isolation=Isolation.HOST,
    )


class NullHarness:
    def __init__(self, mode: NullMode) -> None:
        self._mode: NullMode = mode
        self._info = null_info(mode)

    @property
    def info(self) -> HarnessInfo:
        return self._info

    async def start(self, trial: Trial, box: BoxHandle) -> None:
        return None

    async def turn(self, t: TurnInput) -> TurnOutput:
        return TurnOutput(status=TurnStatus.COMPLETED, answer=self.answer(), session_id=None)

    def answer(self) -> str:
        match self._mode:
            case "claim":
                return CLAIM_ANSWER
            case "refuse":
                return REFUSE_ANSWER
            case _:
                assert_never(self._mode)

    async def stop(self) -> None:
        return None

    async def version(self, box: BoxHandle) -> str:
        return __version__
