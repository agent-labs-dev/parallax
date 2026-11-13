"""The oracle control: replays the task's own solution against the live world.

`solution/oracle.yaml` is declarative (tool calls per step, `{{saved.<name>.<path>}}` threads an
earlier result into later args); `solution/solve.py` is scripted for workspace tasks:
`async def solve(step, text, world, workspace) -> str`. The oracle must score 1 on every task,
so a step the solution does not cover is a harness error, never an empty turn.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import assert_never, cast

import yaml
from pydantic import JsonValue

from parallax import __version__
from parallax.models import (
    ApprovalAnswer,
    Capability,
    CostSource,
    HarnessInfo,
    HarnessKind,
    Isolation,
    JsonObject,
    Model,
    Step,
    StepKind,
    Task,
    Trial,
    TurnInput,
    TurnOutput,
    TurnStatus,
)
from parallax.protocols import BoxHandle
from parallax.task import load_seed, rebind_value
from parallax.world.client import WorldClient

Solve = Callable[[str, str, WorldClient, Path], Awaitable[str]]
ORACLE_APPROVAL_REQUEST = "Run the command this job step needs on the computer"
_SAVED = re.compile(r"\{\{\s*saved\.([A-Za-z0-9_.-]+)\s*\}\}")

ORACLE_INFO = HarnessInfo(
    name="oracle",
    kind=HarnessKind.CONTROL,
    capabilities=frozenset(Capability) - {Capability.CREDENTIALS, Capability.AUTHORIZATION},
    pins_model=False,
    cost_source=CostSource.NONE,
    isolation=Isolation.HOST,
)


class OracleError(RuntimeError):
    """The solution cannot be replayed as written: a task authoring bug."""


class OracleCall(Model):
    tool: str
    args: JsonObject = {}
    save: str | None = None


class OracleStep(Model):
    calls: tuple[OracleCall, ...] = ()
    answer: str


class OracleSpec(Model):
    steps: dict[str, OracleStep]


def load_spec(path: Path) -> OracleSpec:
    return OracleSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_solve(path: Path) -> Solve:
    # Compiled in memory rather than imported: an import writes solution/__pycache__ into the task dir.
    namespace: dict[str, object] = {"__name__": f"parallax_solution_{abs(hash(path))}", "__file__": str(path)}
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    except (OSError, SyntaxError) as exc:
        raise OracleError(f"{path}: not loadable: {exc}") from exc
    solve = namespace.get("solve")
    if not inspect.iscoroutinefunction(solve):
        raise OracleError(f"{path}: must define `async def solve(step, text, world, workspace) -> str`")
    return cast(Solve, solve)


def lookup(saved: dict[str, JsonValue], path: str) -> JsonValue:
    value: JsonValue = saved
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise OracleError(f"{{{{saved.{path}}}}}: no {part!r} in the saved results")
        value = value[part]
    return value


def substitute(value: JsonValue, saved: dict[str, JsonValue]) -> JsonValue:
    """A string that is exactly one placeholder takes the saved value's type; embedded ones are text."""
    match value:
        case str():
            whole = _SAVED.fullmatch(value.strip())
            if whole is not None:
                return lookup(saved, whole[1])
            return _SAVED.sub(lambda m: str(lookup(saved, m[1])), value)
        case dict():
            return {k: substitute(v, saved) for k, v in value.items()}
        case list():
            return [substitute(v, saved) for v in value]
        case _:
            return value


class OracleHarness:
    def __init__(self) -> None:
        self._task: Task | None = None
        self._trial: Trial | None = None
        self._saved: dict[str, JsonValue] = {}
        self._spec: OracleSpec | None = None
        self._solve: Solve | None = None
        self._persona = ("", "")  # the seed's email and the trial's (`rebind_persona`)

    @property
    def info(self) -> HarnessInfo:
        return ORACLE_INFO

    def bind_task(self, task: Task) -> None:
        """The runner hands the oracle its task; no other harness ever sees the solution."""
        self._task = task
        # A task rebound to the product's account (`rebind_persona`) replays, and answers, as that account.
        self._persona = (load_seed(task.path / "world" / "seed.yaml").user.email, task.world.user.email)
        match task.oracle:
            case "yaml":
                spec = load_spec(task.path / "solution" / "oracle.yaml")
                self._spec = OracleSpec.model_validate(rebind_value(spec.model_dump(mode="json"), *self._persona))
            case "py":
                self._solve = load_solve(task.path / "solution" / "solve.py")
            case None:
                raise OracleError(f"task {task.id} has no scripted solution to replay")
            case _:
                assert_never(task.oracle)

    async def start(self, trial: Trial, box: BoxHandle) -> None:
        if self._task is None:
            raise OracleError("oracle started without bind_task(task)")
        self._trial = trial
        self._saved = {}

    def step_name(self, index: int) -> str:
        if (task := self._task) is None:
            raise OracleError("oracle has no task bound")
        names = [t.name for t in task.turns] + [f.name for f in task.meta.fires]
        if not 1 <= index <= len(names):
            raise OracleError(f"turn {index} is outside the task's {len(names)} steps and fires")
        return names[index - 1]

    async def turn(self, t: TurnInput) -> TurnOutput:
        if (trial := self._trial) is None:
            raise OracleError("oracle turn() before start()")
        try:
            name = self.step_name(t.index)
            mcp = trial.world_mcp
            client = WorldClient(mcp.url, trial.env[mcp.token_env])
            async with client:
                if self._solve is not None:
                    answer = rebind_value(await self._solve(name, t.text, client, trial.workspace), *self._persona)
                    out = TurnOutput(status=TurnStatus.COMPLETED, answer=answer, session_id=None)
                else:
                    out = await self.replay(name, client, t.index)
        except OracleError as exc:
            return TurnOutput(status=TurnStatus.HARNESS_ERROR, answer="", session_id=None, error=str(exc))
        if t.approval is None:
            return out
        # A correct run asks once for the step's one local action, and takes the scripted answer.
        approval = ApprovalAnswer(turn=t.index, request=ORACLE_APPROVAL_REQUEST, decision=t.approval)
        return out.model_copy(update={"approvals": (approval,)})

    async def replay(self, name: str, client: WorldClient, index: int) -> TurnOutput:
        spec = self._spec
        if spec is None:
            raise OracleError("oracle has neither oracle.yaml nor solve.py loaded")
        step = spec.steps.get(name)
        if step is None:
            raise OracleError(f"solution/oracle.yaml has no entry for step {name!r}")
        steps: list[Step] = []
        for call in step.calls:
            args = substitute(call.args, self._saved)
            if not isinstance(args, dict):
                raise OracleError(f"{call.tool}: args must stay an object after substitution")
            ok, result = await client.call(call.tool, args)
            steps.append(Step(turn=index, kind=StepKind.TOOL_CALL, name=call.tool, args=args, ok=ok, t_ms=0))
            if call.save is not None:
                if not ok:
                    raise OracleError(f"{call.tool} failed, so it cannot be saved as {call.save!r}: {result}")
                self._saved[call.save] = result
        return TurnOutput(status=TurnStatus.COMPLETED, answer=step.answer, session_id=None, steps=tuple(steps))

    async def stop(self) -> None:
        return None

    async def version(self, box: BoxHandle) -> str:
        return __version__
