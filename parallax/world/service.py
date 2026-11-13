"""Service base for stateful mock services.

A service is a class whose tools are `async def <method>(self, args: <ArgsModel>) -> JsonValue`
methods decorated with `@tool`. The tool name is `<service>_<method>` and its input schema
is the args model's JSON Schema, so the schema an agent sees and the validation it gets
can never drift apart.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import FunctionType
from typing import ClassVar, Literal, TypeVar, get_type_hints

from pydantic import BaseModel, JsonValue, ValidationError

from parallax.models import JsonObject, Model
from parallax.protocols import WorldState

ServiceErrorKind = Literal["validation", "not_found", "conflict"]


class ServiceError(Exception):
    """An agent-attributable failure; any other exception from a service is an eval bug."""

    def __init__(self, kind: ServiceErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: ServiceErrorKind = kind
        self.message = message


class ToolSpec(Model):
    name: str
    description: str
    input_schema: JsonObject
    mutates: bool
    # Only meaningful when `mutates`: the write adds state and never changes or removes existing state.
    # It is MCP's `destructiveHint` inverted, and a write defaults to destructive, as MCP's does.
    additive: bool = False


F = TypeVar("F", bound=Callable[..., Awaitable[JsonValue]])


@dataclass(frozen=True)
class ToolMeta:
    mutates: bool
    additive: bool
    description: str


@dataclass(frozen=True)
class BoundTool:
    method: str
    fn: Callable[[Service, BaseModel], Awaitable[JsonValue]]
    args_model: type[BaseModel]
    meta: ToolMeta


# Keyed by the undecorated function so `@tool` returns it unchanged (no wrapper, no
# attribute smuggling); Service.__init_subclass__ reads it once per class.
_TOOL_META: dict[object, ToolMeta] = {}


def tool(*, mutates: bool, description: str, additive: bool = False) -> Callable[[F], F]:
    if additive and not mutates:
        raise TypeError("@tool(additive=True) requires mutates=True: a read has nothing to add")

    def register(fn: F) -> F:
        _TOOL_META[fn] = ToolMeta(mutates=mutates, additive=additive, description=description)
        return fn

    return register


def _args_model(fn: FunctionType) -> type[BaseModel]:
    if not inspect.iscoroutinefunction(fn):
        raise TypeError(f"@tool {fn.__qualname__} must be `async def`")
    params = list(inspect.signature(fn).parameters)
    if params != ["self", "args"]:
        raise TypeError(f"@tool {fn.__qualname__} must take exactly (self, args), got {params}")
    hint = get_type_hints(fn).get("args")
    if not (isinstance(hint, type) and issubclass(hint, BaseModel)):
        raise TypeError(f"@tool {fn.__qualname__}: `args` must be annotated with a pydantic model, got {hint!r}")
    return hint


class Service:
    name: ClassVar[str]  # "mail"
    tables: ClassVar[tuple[str, ...]]  # ("messages", "drafts", "sent", "labels")
    tools: ClassVar[dict[str, BoundTool]]  # method -> tool, in definition order

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        for attr in ("name", "tables"):
            if attr not in vars(cls):
                raise TypeError(f"Service {cls.__qualname__} must define ClassVar `{attr}`")
        collected: dict[str, BoundTool] = {}
        for method, value in vars(cls).items():
            meta = _TOOL_META.get(value) if isinstance(value, FunctionType) else None
            if meta is not None and isinstance(value, FunctionType):
                collected[method] = BoundTool(method=method, fn=value, args_model=_args_model(value), meta=meta)
        if not collected:
            raise TypeError(f"Service {cls.__qualname__} defines no @tool methods")
        cls.tools = collected

    def __init__(self, world: WorldState) -> None:
        self.world = world

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(
                name=f"{self.name}_{t.method}",
                description=t.meta.description,
                input_schema=t.args_model.model_json_schema(),
                mutates=t.meta.mutates,
                additive=t.meta.additive,
            )
            for t in self.tools.values()
        )

    async def call(self, method: str, args: JsonObject) -> JsonValue:
        bound = self.tools.get(method)
        if bound is None:
            raise ServiceError("validation", f"unknown tool {self.name}_{method}")
        try:
            parsed = bound.args_model.model_validate(args)
        except ValidationError as exc:
            # vendor-style field errors: short, one per field, no pydantic URLs
            parts = [f"{'.'.join(str(p) for p in err['loc']) or '<args>'}: {err['msg']}" for err in exc.errors()]
            raise ServiceError("validation", "; ".join(parts)) from exc
        return await bound.fn(self, parsed)
