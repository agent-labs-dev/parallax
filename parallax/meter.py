"""Per-trial LLM metering proxy.

Harnesses in a box are pointed at `/{trial_key}/openai/v1/...` or `/{trial_key}/anthropic/v1/...`
with the trial key as their "API key". The meter swaps in the host's real upstream key, so a
provider key never enters the box, and it reads usage off every response (JSON body or SSE
stream) to price it with genai-prices. Cost comes from what the provider served, not from what
the harness claims, and `actual_model` is what the provider says answered.

Token convention for `Usage.input`: TOTAL input tokens including cache reads and writes (the
OpenAI and genai-prices convention); Anthropic's uncached `input_tokens` is widened to match.
"""

from __future__ import annotations

import asyncio
import fcntl
import functools
import hmac
import json
import logging
import math
import os
import re
import secrets
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, assert_never

import genai_prices
import httpx
import uvicorn
from pydantic import Field, JsonValue, SecretStr, TypeAdapter
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from parallax.models import CostSource, LlmEndpoint, Model, Usage

logger = logging.getLogger(__name__)

ADMIN_TOKEN_ENV = "PARALLAX_METER_ADMIN_TOKEN"
DEFAULT_UPSTREAM = {"openai": "https://openrouter.ai/api/v1", "anthropic": "https://api.anthropic.com/v1"}

# Hop-by-hop and auth headers are never forwarded: the box's credentials are the trial key,
# and httpx re-encodes bodies itself.
DROP_REQUEST_HEADERS = frozenset(
    {"host", "content-length", "authorization", "x-api-key", "accept-encoding", "connection", "transfer-encoding"}
)
KEEP_RESPONSE_HEADERS = frozenset({"content-type", "retry-after", "request-id", "x-request-id", "openai-processing-ms"})


class Wire(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class Endpoint(StrEnum):
    """What an allowed upstream endpoint does, which decides whether its 2xx is metered."""

    GENERATION = "generation"  # returns usage; priced and recorded
    FREE = "free"  # token counting and model listing: no generation, so nothing to record


# The only (method, path) pairs the meter forwards, relative to the wire's /v1. The upstream key
# carries the host account's full authority (files, batches, fine-tuning, admin), so anything a
# harness does not need for inference is refused before the key is attached. On the OpenAI wire a
# harness uses Chat Completions (hermes, openclaw) or Responses; on the Anthropic wire, Messages and
# count_tokens. `models` is a read-only listing hermes probes at startup for context windows. A
# stored-response GET stays refused: it would read back responses outside the trial.
ALLOWED_ENDPOINTS: Mapping[Wire, Mapping[tuple[str, str], Endpoint]] = {
    Wire.OPENAI: {
        ("POST", "chat/completions"): Endpoint.GENERATION,
        ("POST", "responses"): Endpoint.GENERATION,
        ("POST", "responses/input_tokens"): Endpoint.FREE,
        ("GET", "models"): Endpoint.FREE,
    },
    Wire.ANTHROPIC: {
        ("POST", "messages"): Endpoint.GENERATION,
        ("POST", "messages/count_tokens"): Endpoint.FREE,
    },
}


# Tool types a harness executes itself, per wire. Every other tool type runs on the provider's
# servers (web search and fetch, code execution, hosted MCP, file search, image generation) and
# would give the box live web access through the meter, breaking the no-web isolation, and
# add charges the meter cannot price, so a request carrying one is refused. Measured against
# conf-mcp-echo in docker: Anthropic-wire harnesses send only untyped custom tools, and a Responses
# client may send `function` plus `namespace`, a named group of its function tools (its MCP
# servers), whose members are checked the same way. `bash` and
# `text_editor` are Anthropic-defined but run on the client; OpenAI's `local_shell` likewise.
# Anthropic versions its types with a date suffix (`bash_20250124`), stripped before lookup.
CLIENT_TOOL_TYPES: Mapping[Wire, frozenset[str]] = {
    Wire.ANTHROPIC: frozenset({"custom", "bash", "text_editor"}),
    Wire.OPENAI: frozenset({"function", "custom", "local_shell", "namespace"}),
}
# Top-level request fields that turn on provider-side tools without a `tools` entry.
SERVER_TOOL_FIELDS: Mapping[Wire, frozenset[str]] = {
    Wire.ANTHROPIC: frozenset({"mcp_servers", "container"}),
    Wire.OPENAI: frozenset({"plugins", "web_search_options"}),  # OpenRouter plugins; Chat Completions search
}
ONLINE_MODEL_SUFFIX = ":online"  # OpenRouter's web-search variant of any model
TOOL_TYPE_VERSION = re.compile(r"_\d{8}$")
# How long a report waits for streams whose client hung up to finish (see `pump`).
STREAM_DRAIN_WAIT_S = 120.0


class MeterError(StrEnum):
    ENDPOINT_NOT_ALLOWED = "endpoint_not_allowed"
    INVALID_TRIAL_KEY = "invalid_trial_key"
    SERVER_TOOL_NOT_ALLOWED = "server_tool_not_allowed"
    UNREADABLE_REQUEST = "unreadable_request"
    UPSTREAM_DISABLED = "upstream_disabled"
    UPSTREAM_UNREACHABLE = "upstream_unreachable"

    @property
    def anthropic_type(self) -> str:
        match self:
            case MeterError.INVALID_TRIAL_KEY:
                return "authentication_error"
            case MeterError.ENDPOINT_NOT_ALLOWED | MeterError.SERVER_TOOL_NOT_ALLOWED:
                return "permission_error"
            case MeterError.UNREADABLE_REQUEST:
                return "invalid_request_error"
            case MeterError.UPSTREAM_DISABLED | MeterError.UPSTREAM_UNREACHABLE:
                return "api_error"
            case _:
                assert_never(self)


class Upstream(Model):
    base_url: str  # includes the version segment, e.g. https://openrouter.ai/api/v1
    api_key: SecretStr | None  # None => this wire is disabled and answers 503


class MeterReport(Model):
    usage: tuple[Usage, ...]  # one per model: the one the response named, else the one requested
    # Distinct models the upstream RESPONSES named, first-seen order. The requested model is never
    # evidence of what served: a routing proxy can substitute one and still echo the request.
    actual_models: tuple[str, ...]
    upstream_errors: int  # non-2xx upstream responses and transport failures
    unpriced_requests: int  # 2xx responses whose usage or price could not be determined
    unobserved_requests: int = 0  # metered 2xx responses that did not name the model that served them


@dataclass
class ModelTally:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    requests: int = 0
    cost_usd: float = 0.0
    unpriced: bool = False  # one unknown price makes the model's cost unknown, never an undercount

    def usage(self, model: str) -> Usage:
        return Usage(
            model=model,
            input=self.input,
            output=self.output,
            cache_read=self.cache_read,
            cache_write=self.cache_write,
            reasoning=self.reasoning,
            requests=self.requests,
            cost_usd=None if self.unpriced else self.cost_usd,
            source=CostSource.METERED,
        )


@dataclass
class TrialMeter:
    models: dict[str, ModelTally] = field(default_factory=dict)
    upstream_errors: int = 0
    unpriced_requests: int = 0
    served: dict[str, None] = field(default_factory=dict)  # ordered set of response-named models
    unobserved_requests: int = 0
    # Upstream streams still being read, whether or not their client is still there (see `pump`),
    # each with the accumulator it records into; None once nothing may record it (unmetered, or
    # already counted as unpriced by `settle`).
    open_streams: dict[asyncio.Task[None], UsageAccumulator | None] = field(default_factory=dict)

    def report(self) -> MeterReport:
        return MeterReport(
            usage=tuple(t.usage(m) for m, t in self.models.items()),
            actual_models=tuple(self.served),
            upstream_errors=self.upstream_errors,
            unpriced_requests=self.unpriced_requests,
            unobserved_requests=self.unobserved_requests,
        )


@dataclass(frozen=True)
class Observed:
    """Normalised usage of one upstream response."""

    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int
    reasoning: int


# ---------------------------------------------------------------------------
# usage extraction (pure)
# ---------------------------------------------------------------------------


def flatten_ints(obj: JsonValue, prefix: str = "") -> dict[str, int]:
    """`{"a": {"b": 3}}` -> `{"a.b": 3}`; non-int leaves are dropped (usage fields are ints)."""
    out: dict[str, int] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.update(flatten_ints(value, f"{prefix}{key}."))
    elif isinstance(obj, int) and not isinstance(obj, bool):
        out[prefix.rstrip(".")] = obj
    return out


class UsageAccumulator:
    """Folds every usage object seen in one response.

    Streams report usage cumulatively (Anthropic `message_start` then `message_delta`, OpenAI's
    final chunk, Responses' `response.completed`), so the latest value of each field wins.
    """

    def __init__(self, wire: Wire, request_model: str | None) -> None:
        self.wire = wire
        self.model = request_model  # labels usage and prices it; the request's model until a response names one
        self.served: str | None = None  # only what a response named: the evidence for board placement
        self.fields: dict[str, int] = {}

    def feed(self, event: JsonValue) -> None:
        if not isinstance(event, dict):
            return
        for holder in (event, event.get("response"), event.get("message")):
            if not isinstance(holder, dict):
                continue
            model = holder.get("model")
            if isinstance(model, str) and model:
                self.model = self.served = model
            usage = holder.get("usage")
            if isinstance(usage, dict):
                self.fields.update(flatten_ints(usage))

    def observed(self) -> Observed | None:
        if not self.fields or self.model is None:
            return None
        f = self.fields.get
        match self.wire:
            case Wire.OPENAI:
                if "input_tokens" in self.fields:  # Responses API (OpenRouter adds cache_write_tokens)
                    return Observed(
                        model=self.model,
                        input=f("input_tokens", 0),
                        output=f("output_tokens", 0),
                        cache_read=f("input_tokens_details.cached_tokens", 0),
                        cache_write=f("input_tokens_details.cache_write_tokens", 0),
                        reasoning=f("output_tokens_details.reasoning_tokens", 0),
                    )
                return Observed(  # Chat Completions (OpenRouter adds cache_write_tokens)
                    model=self.model,
                    input=f("prompt_tokens", 0),
                    output=f("completion_tokens", 0),
                    cache_read=f("prompt_tokens_details.cached_tokens", 0),
                    cache_write=f("prompt_tokens_details.cache_write_tokens", 0),
                    reasoning=f("completion_tokens_details.reasoning_tokens", 0),
                )
            case Wire.ANTHROPIC:
                cache_read = f("cache_read_input_tokens", 0)
                cache_write = f("cache_creation_input_tokens", 0)
                return Observed(
                    model=self.model,
                    input=f("input_tokens", 0) + cache_read + cache_write,
                    output=f("output_tokens", 0),
                    cache_read=cache_read,
                    cache_write=cache_write,
                    reasoning=0,
                )
            case _:
                assert_never(self.wire)


class SseParser:
    """Incremental `data:` line parser; tolerant of chunk boundaries anywhere."""

    def __init__(self) -> None:
        self.buffer = b""

    def feed(self, chunk: bytes) -> list[JsonValue]:
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split(b"\n")
        return [event for line in lines if (event := parse_sse_line(line)) is not None]

    def finish(self) -> list[JsonValue]:
        tail, self.buffer = self.buffer, b""
        event = parse_sse_line(tail)
        return [] if event is None else [event]


def parse_sse_line(line: bytes) -> JsonValue:
    text = line.decode("utf-8", errors="replace").strip()
    if not text.startswith("data:"):
        return None
    payload = text[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def price(observed: Observed, upstream_url: str) -> float | None:
    """genai-prices keyed by the upstream URL; unknown provider or model => None, never 0."""
    usage = genai_prices.Usage(
        input_tokens=observed.input,
        output_tokens=observed.output,
        cache_read_tokens=observed.cache_read,
        cache_write_tokens=observed.cache_write,
    )
    try:
        return float(genai_prices.calc_price(usage, observed.model, provider_api_url=upstream_url).total_price)
    except LookupError:
        logger.warning("meter: no price for model %r at %s; cost recorded as unknown", observed.model, upstream_url)
        return None


def record_unpriced(trial: TrialMeter, acc: UsageAccumulator) -> str:
    trial.unpriced_requests += 1
    model = acc.model or "unknown"
    tally = trial.models.setdefault(model, ModelTally())
    tally.requests += 1
    tally.unpriced = True
    return model


def record(trial: TrialMeter, acc: UsageAccumulator, upstream_url: str) -> None:
    if acc.served is None:
        trial.unobserved_requests += 1
    else:
        trial.served.setdefault(acc.served)
    observed = acc.observed()
    if observed is None:
        model = record_unpriced(trial, acc)
        logger.warning("meter: 2xx response without usage for model %r; cost recorded as unknown", model)
        return
    tally = trial.models.setdefault(observed.model, ModelTally())
    tally.requests += 1
    tally.input += observed.input
    tally.output += observed.output
    tally.cache_read += observed.cache_read
    tally.cache_write += observed.cache_write
    tally.reasoning += observed.reasoning
    cost = price(observed, upstream_url)
    if cost is None:
        trial.unpriced_requests += 1
        tally.unpriced = True
    else:
        tally.cost_usd += cost


# ---------------------------------------------------------------------------
# error shapes
# ---------------------------------------------------------------------------


def wire_error(wire: Wire, status: int, kind: MeterError, message: str) -> JSONResponse:
    """An error body the harness's own SDK can parse, so it surfaces the message verbatim."""
    match wire:
        case Wire.OPENAI:
            body: JsonValue = {"error": {"message": message, "type": kind.value, "param": None, "code": kind.value}}
        case Wire.ANTHROPIC:
            body = {"type": "error", "error": {"type": kind.anthropic_type, "message": message}}
        case _:
            assert_never(wire)
    return JSONResponse(body, status_code=status)


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------


def app(
    admin_token: str,
    upstreams: Mapping[Wire, Upstream],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """The meter ASGI app. `transport` exists so tests can mount an httpx MockTransport upstream."""
    trials: dict[str, TrialMeter] = {}
    clients: list[httpx.AsyncClient] = []

    def client() -> httpx.AsyncClient:
        if not clients:
            timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
            clients.append(httpx.AsyncClient(transport=transport, timeout=timeout))
        return clients[0]

    def admin_ok(request: Request) -> bool:
        return hmac.compare_digest(str(request.path_params["admin"]), admin_token)

    async def health(_: Request) -> Response:
        return JSONResponse({"ok": True})

    async def control(request: Request) -> Response:
        if not admin_ok(request):
            return Response(status_code=404)
        key = str(request.path_params["trial_key"])
        match request.method:
            case "PUT":
                if key in trials:
                    return JSONResponse({"error": "trial key already registered"}, status_code=409)
                trials[key] = TrialMeter()
                return Response(status_code=204)
            case "GET":
                trial = trials.get(key)
                if trial is None:
                    return JSONResponse({"error": "unknown trial key"}, status_code=404)
                await settle(trial)
                return Response(trial.report().model_dump_json(), media_type="application/json")
            case other:
                return Response(f"method {other} not allowed", status_code=405)

    async def proxy(wire: Wire, request: Request) -> Response:
        trial = trials.get(str(request.path_params["trial_key"]))
        if trial is None:
            return wire_error(wire, 401, MeterError.INVALID_TRIAL_KEY, "parallax meter: unknown trial key")
        path = str(request.path_params["path"]).strip("/")
        endpoint = allowed_endpoint(wire, request.method, path)
        if endpoint is None:
            logger.warning(
                "meter: refused %s %s on the %s wire: not an allowed inference endpoint", request.method, path, wire
            )
            return wire_error(
                wire,
                403,
                MeterError.ENDPOINT_NOT_ALLOWED,
                f"parallax meter: {request.method} /v1/{path} is not an allowed inference endpoint on the {wire} wire",
            )
        raw = await request.body()
        parsed = parse_json(raw) if raw else None
        if endpoint is Endpoint.GENERATION:
            if not isinstance(parsed, dict):
                # A body the meter cannot read (compressed, not JSON) could carry server tools unseen.
                logger.warning("meter: refused a %s request on the %s wire: body is not a JSON object", path, wire)
                return wire_error(
                    wire,
                    400,
                    MeterError.UNREADABLE_REQUEST,
                    f"parallax meter: {path} needs an uncompressed JSON object body",
                )
            if offending := server_tools(wire, parsed):
                logger.warning("meter: refused a %s request on the %s wire with server tools %s", path, wire, offending)
                return wire_error(
                    wire,
                    403,
                    MeterError.SERVER_TOOL_NOT_ALLOWED,
                    f"parallax meter: provider-side tools are not allowed (trials have no live web access): "
                    f"{', '.join(offending)}",
                )
        body, request_model, background = prepare_body(path, raw, parsed)
        if background:
            # A background response generates after this request returns, so its usage never
            # passes through the meter and cannot be recorded; its result is only readable
            # through the refused GET responses/{id}, so no harness can use one anyway.
            logger.warning("meter: refused a background %s on the %s wire: it cannot be metered", path, wire)
            return wire_error(
                wire,
                403,
                MeterError.ENDPOINT_NOT_ALLOWED,
                "parallax meter: background responses cannot be metered; send the request without background",
            )
        upstream = upstreams[wire]
        if upstream.api_key is None:
            return wire_error(
                wire, 503, MeterError.UPSTREAM_DISABLED, f"parallax meter: no upstream key configured for {wire}"
            )
        url = f"{upstream.base_url.rstrip('/')}/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST_HEADERS}
        headers.update(auth_headers(wire, upstream.api_key))
        # Streams are relayed byte for byte and parsed for usage on the way; httpx would otherwise
        # ask for gzip, and a compressed SSE body is neither parseable here nor labelled for the client.
        headers["accept-encoding"] = "identity"
        upstream_request = client().build_request(request.method, url, headers=headers, content=body)
        try:
            response = await client().send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            trial.upstream_errors += 1
            logger.warning("meter: upstream %s unreachable: %s", upstream.base_url, exc)
            return wire_error(
                wire, 502, MeterError.UPSTREAM_UNREACHABLE, f"parallax meter: upstream unreachable ({exc})"
            )
        out_headers = {k: v for k, v in response.headers.items() if k.lower() in KEEP_RESPONSE_HEADERS}
        acc = UsageAccumulator(wire, request_model)
        ok = response.is_success
        metered = ok and endpoint is Endpoint.GENERATION
        if not ok:
            trial.upstream_errors += 1
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
            events = SseStream(acc)
            task = asyncio.create_task(pump(response, events, chunks, trial if metered else None, upstream.base_url))
            trial.open_streams[task] = acc if metered else None
            task.add_done_callback(lambda t: trial.open_streams.pop(t, None))
            return StreamingResponse(relay(chunks), status_code=response.status_code, headers=out_headers)
        try:
            content = await response.aread()
        finally:
            await response.aclose()
        if metered:
            acc.feed(parse_json(content))
            record(trial, acc, upstream.base_url)
        return Response(content, status_code=response.status_code, headers=out_headers)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            for trial in trials.values():
                for task in trial.open_streams:
                    task.cancel()
            for c in clients:
                await c.aclose()

    return Starlette(
        routes=[
            Route("/_health", health, methods=["GET"]),
            Route("/_meter/{admin}/{trial_key}", control, methods=["GET", "PUT"]),
            *(
                Route(
                    f"/{{trial_key}}/{wire.value}/v1/{{path:path}}",
                    functools.partial(proxy, wire),
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                )
                for wire in Wire
            ),
        ],
        lifespan=lifespan,
    )


class SseStream:
    """Folds usage from an SSE body and passes its bytes through untouched."""

    def __init__(self, acc: UsageAccumulator) -> None:
        self.acc = acc
        self.parser = SseParser()

    def feed(self, chunk: bytes) -> bytes:
        for event in self.parser.feed(chunk):
            self.acc.feed(event)
        return chunk

    def finish(self) -> bytes:
        for event in self.parser.finish():
            self.acc.feed(event)
        return b""


async def pump(
    response: httpx.Response,
    events: SseStream,
    chunks: asyncio.Queue[bytes | None],
    trial: TrialMeter | None,
    upstream_url: str,
) -> None:
    """Reads an upstream stream to its end in its own task, queueing what the client is sent.

    Usage arrives only in a stream's last events, so a stream the client abandons (a harness
    killed at its timeout, a side call it gives up on) would otherwise leave the trial's cost
    unknown. The provider bills a generation the client stopped reading, so the meter keeps
    reading it and records what it cost; `None` on the queue ends the client's copy.
    """
    try:
        async for chunk in response.aiter_raw():
            if out := events.feed(chunk):
                chunks.put_nowait(out)
        if out := events.finish():
            chunks.put_nowait(out)
    except httpx.HTTPError as exc:
        logger.warning("meter: upstream stream broke off: %s", exc)
    finally:
        chunks.put_nowait(None)
        await response.aclose()
        if trial is not None:
            if trial.open_streams.get(asyncio.current_task()) is None:
                # `settle` already counted it as unpriced in a report that has been served.
                logger.warning("meter: stream ended after its trial was reported; usage not recorded")
            else:
                record(trial, events.acc, upstream_url)


async def relay(chunks: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
    """The client's copy of a pumped stream; leaving early does not stop the pump."""
    while (chunk := await chunks.get()) is not None:
        yield chunk


async def settle(trial: TrialMeter) -> None:
    """Waits (bounded) for streams to be read to their end, so their usage is in the report.

    A metered stream still open at the deadline is counted as an unpriced request, and its pump
    no longer records, so the report served now stays the trial's final word.
    """
    if not trial.open_streams:
        return
    _, pending = await asyncio.wait(set(trial.open_streams), timeout=STREAM_DRAIN_WAIT_S)
    if pending:
        for task in pending:
            if (acc := trial.open_streams.get(task)) is not None:
                record_unpriced(trial, acc)
                trial.open_streams[task] = None
        logger.warning(
            "meter: %d stream(s) still open after %.0fs; their cost is recorded as unknown",
            len(pending),
            STREAM_DRAIN_WAIT_S,
        )


def allowed_endpoint(wire: Wire, method: str, path: str) -> Endpoint | None:
    """The allowlisted endpoint for a request, or None to refuse it (`path` is relative to /v1)."""
    return ALLOWED_ENDPOINTS[wire].get((method.upper(), path))


def parse_json(content: bytes) -> JsonValue:
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def server_tools(wire: Wire, body: Mapping[str, JsonValue]) -> list[str]:
    """Every provider-side tool, tool-enabling field or web-search model in a generation request."""
    offending = [
        name for name in sorted(SERVER_TOOL_FIELDS[wire]) if body.get(name) is not None and body.get(name) != []
    ]
    fallbacks = body.get("models")  # OpenRouter's fallback list
    models = [body.get("model"), *(fallbacks if isinstance(fallbacks, list) else [])]
    offending += [f"model {m}" for m in models if isinstance(m, str) and m.endswith(ONLINE_MODEL_SUFFIX)]
    return offending + server_tool_types(wire, body.get("tools"))


def server_tool_types(wire: Wire, tools: JsonValue) -> list[str]:
    if tools is None:
        return []
    if not isinstance(tools, list):
        return ["tools (not a list)"]
    offending: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            offending.append("tools entry (not an object)")
            continue
        kind = tool.get("type")
        if kind is None and wire is Wire.ANTHROPIC:
            continue  # an untyped Anthropic tool is a custom tool
        if not isinstance(kind, str):
            offending.append(f"tool type {kind}")
            continue
        # Only Anthropic dates its tool types; stripping elsewhere would let `namespace_20250101`
        # pass the allowlist without its members being checked.
        base = TOOL_TYPE_VERSION.sub("", kind) if wire is Wire.ANTHROPIC else kind
        if base not in CLIENT_TOOL_TYPES[wire]:
            offending.append(f"tool type {kind}")
        elif base == "namespace":
            offending += server_tool_types(wire, tool.get("tools"))
    return offending


def prepare_body(path: str, raw: bytes, body: JsonValue) -> tuple[bytes, str | None, bool]:
    """Returns the body to forward, the requested model, and whether it asks for a background response.

    Streaming Chat Completions omit usage unless `stream_options.include_usage` is set, which
    would leave the request unmetered, so the meter asks for it.
    """
    if not isinstance(body, dict):
        return raw, None, False
    # Forward what the meter parsed, not the raw bytes: a body with duplicate keys
    # (`"tools": [], "tools": [<web_search>]`) would otherwise be checked as one request and read
    # upstream, by a parser that keeps a different duplicate, as another.
    forwarded = json.dumps(body).encode()
    model = body.get("model")
    request_model = model if isinstance(model, str) else None
    background = path == "responses" and body.get("background") is True
    if path == "chat/completions" and body.get("stream") is True:
        options = body.get("stream_options")
        if not isinstance(options, dict) or options.get("include_usage") is not True:
            patched = {**(options if isinstance(options, dict) else {}), "include_usage": True}
            return json.dumps({**body, "stream_options": patched}).encode(), request_model, background
    return forwarded, request_model, background


def auth_headers(wire: Wire, key: SecretStr) -> dict[str, str]:
    match wire:
        case Wire.OPENAI:
            return {"authorization": f"Bearer {key.get_secret_value()}"}
        case Wire.ANTHROPIC:
            return {"x-api-key": key.get_secret_value()}
        case _:
            assert_never(wire)


# ---------------------------------------------------------------------------
# configuration, serving and the runner-side client
# ---------------------------------------------------------------------------


def upstreams_from_env(env: Mapping[str, str]) -> dict[Wire, Upstream]:
    """`PARALLAX_UPSTREAM_{OPENAI,ANTHROPIC}` (base URL) and `..._KEY` (the host's real key).

    A wire without a key is served as disabled (503) rather than refused at boot, so a run that
    only uses one wire needs only that key; the omission is logged.
    """
    upstreams: dict[Wire, Upstream] = {}
    for wire in Wire:
        prefix = f"PARALLAX_UPSTREAM_{wire.value.upper()}"
        base_url = env.get(prefix) or DEFAULT_UPSTREAM[wire.value]
        key = env.get(f"{prefix}_KEY")
        if not key:
            logger.warning("meter: %s_KEY is not set; the %s wire answers 503", prefix, wire.value)
        upstreams[wire] = Upstream(base_url=base_url, api_key=SecretStr(key) if key else None)
    return upstreams


def parse_http(http: str) -> tuple[str, int]:
    host, sep, port = http.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"expected HOST:PORT, got {http!r}")
    return host, int(port)


def serve(http: str, admin_token: str, upstreams: Mapping[Wire, Upstream]) -> None:
    host, port = parse_http(http)
    uvicorn.run(app(admin_token, upstreams), host=host, port=port, log_level="warning")


def serve_from_env(http: str) -> None:
    """Entry for `python -m parallax.meter HOST:PORT`; the admin token comes from the env, never argv."""
    admin = os.environ.get(ADMIN_TOKEN_ENV)
    if not admin:
        raise SystemExit(f"{ADMIN_TOKEN_ENV} must be set")
    serve(http, admin, upstreams_from_env(os.environ))


def new_trial_key() -> str:
    return secrets.token_urlsafe(32)


def llm_endpoint(meter_url: str, trial_key: str) -> LlmEndpoint:
    """The URLs a harness sees; `meter_url` is the meter as reachable from the box.

    Each follows its vendor SDK's base-URL convention: OpenAI clients take the `/v1` base, while
    Anthropic clients (Claude Code included) append `/v1/messages` to a base without it.
    """
    base = meter_url.rstrip("/")
    return LlmEndpoint(
        openai_base_url=f"{base}/{trial_key}/openai/v1",
        anthropic_base_url=f"{base}/{trial_key}/anthropic",
        api_key=trial_key,
    )


class MeterClient:
    """Runner-side control client (`meter_url` as reachable from the runner)."""

    def __init__(self, meter_url: str, admin_token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = f"{meter_url.rstrip('/')}/_meter/{admin_token}"
        self.transport = transport

    async def register(self, trial_key: str) -> None:
        """Opens the trial's tally. The meter records spend and never refuses it: the product under
        test owns its own limits."""
        async with httpx.AsyncClient(transport=self.transport, timeout=10.0) as c:
            r = await c.put(f"{self.base}/{trial_key}")
            r.raise_for_status()

    async def report(self, trial_key: str) -> MeterReport:
        """Raises httpx.HTTPError when the meter is unreachable; the runner grades that INVALID."""
        # The meter answers once abandoned streams are read to their end (bounded by STREAM_DRAIN_WAIT_S).
        async with httpx.AsyncClient(transport=self.transport, timeout=STREAM_DRAIN_WAIT_S + 30.0) as c:
            r = await c.get(f"{self.base}/{trial_key}")
            r.raise_for_status()
            return MeterReport.model_validate_json(r.content)


if __name__ == "__main__":
    serve_from_env(sys.argv[1])


# ---------------------------------------------------------------------------
# campaign budget: admission of each trial against the campaign's spend limit
# ---------------------------------------------------------------------------

BUDGET_POLICY = "start-gate-v1"
"""The campaign limit gates starts only. It is never handed to a running agent and never stops one."""


class BudgetRefused(RuntimeError):
    """The campaign's limit cannot hold another trial's reservation, and no trial in flight can free room."""


class _BudgetHeader(Model):
    event: Literal["header"] = "header"
    policy: str
    limit_usd: float
    reservation_usd: float


class _BudgetReserve(Model):
    event: Literal["reserve"] = "reserve"
    hold: str  # one per physical attempt
    trial: str
    amount_usd: float


class _BudgetSettle(Model):
    event: Literal["settle"] = "settle"
    hold: str
    charge_usd: float
    known: bool  # False: the trial's cost was unknown, so the charge is its full reservation


_BudgetEvent = Annotated[_BudgetHeader | _BudgetReserve | _BudgetSettle, Field(discriminator="event")]
_BUDGET_EVENT: TypeAdapter[_BudgetHeader | _BudgetReserve | _BudgetSettle] = TypeAdapter(_BudgetEvent)


@dataclass(frozen=True)
class Hold:
    """A trial's reservation, returned by `CampaignBudget.reserve` and given back to `settle`."""

    id: str
    trial: str
    amount_usd: float


class CampaignBudget:
    """Admits each trial against a campaign spend limit, across a run and every resume of it.

    Before a trial starts it reserves `reservation_usd`, an estimate of one trial's cost, and it may start only while
    everything already committed plus that hold fits the limit; otherwise it waits for a trial in flight to settle, and
    is refused when none is in flight. When the trial ends its cost replaces the hold. A cost above the hold is recorded
    as it is and stops nothing: it leaves less room for the trials after it. An unknown cost, and a trial interrupted
    before it settled, keep the full hold, so a resumed campaign never forgets money that may have been spent.

    Every step is appended to the journal (`budget.jsonl` in the run directory) and synced before it takes effect. One
    process owns a journal at a time (an exclusive lock on it); a journal written under other limits is refused.
    """

    def __init__(self, limit_usd: float, reservation_usd: float, journal: Path) -> None:
        if not all(math.isfinite(v) and v > 0 for v in (limit_usd, reservation_usd)):
            raise ValueError("a campaign budget and its per-trial reservation must be positive, finite dollars")
        self.limit, self.reservation, self.journal = limit_usd, reservation_usd, journal
        journal.parent.mkdir(parents=True, exist_ok=True)
        self._lock = journal.open("a")  # the journal is its own lock, so the run directory holds no lock file
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise ValueError(f"another process owns the campaign budget at {journal}") from None
        self.charges: dict[str, float] = {}  # hold -> what it currently commits
        self._active: set[str] = set()
        self._condition = asyncio.Condition()
        try:
            self._replay()
        except BaseException:
            self._lock.close()
            raise

    @classmethod
    def from_journal(cls, journal: Path) -> CampaignBudget:
        """Reopen a budget using the immutable limit and reservation in its journal header."""
        lines = [line for line in journal.read_text().splitlines() if line.strip()]
        if not lines:
            raise ValueError(f"the campaign budget at {journal} is empty")
        header = _BUDGET_EVENT.validate_json(lines[0])
        if not isinstance(header, _BudgetHeader):
            raise ValueError(f"the campaign budget at {journal} has no header")
        return cls(header.limit_usd, header.reservation_usd, journal)

    def _replay(self) -> None:
        header = _BudgetHeader(policy=BUDGET_POLICY, limit_usd=self.limit, reservation_usd=self.reservation)
        if not self.journal.read_text().strip():
            self._append(header)
            return
        lines = [line for line in self.journal.read_text().splitlines() if line.strip()]
        first = _BUDGET_EVENT.validate_json(lines[0])
        if first != header:
            raise ValueError(f"the campaign budget at {self.journal} was opened under {first}; resume it with the same")
        for line in lines[1:]:
            event = _BUDGET_EVENT.validate_json(line)
            match event:
                case _BudgetReserve():
                    self.charges[event.hold] = event.amount_usd
                case _BudgetSettle():
                    if event.hold not in self.charges:
                        raise ValueError(f"budget journal settles hold {event.hold}, which it never reserved")
                    self.charges[event.hold] = event.charge_usd
                case _BudgetHeader():
                    raise ValueError(f"budget journal {self.journal} holds a second header")
                case _:
                    assert_never(event)

    @property
    def committed(self) -> float:
        return math.fsum(self.charges.values())

    def _append(self, event: _BudgetHeader | _BudgetReserve | _BudgetSettle) -> None:
        descriptor = os.open(self.journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, (event.model_dump_json() + "\n").encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def reserve(self, trial: str) -> Hold:
        """Hold one trial's reservation before it starts; waits while trials in flight may free room."""
        async with self._condition:
            while self.committed + self.reservation > self.limit + 1e-9:
                if not self._active:
                    raise BudgetRefused(
                        f"${self.committed:.4f} of ${self.limit:.4f} is committed; "
                        f"another trial's ${self.reservation:.4f} reservation does not fit"
                    )
                await self._condition.wait()
            hold = Hold(id=secrets.token_hex(8), trial=trial, amount_usd=self.reservation)
            self._append(_BudgetReserve(hold=hold.id, trial=trial, amount_usd=hold.amount_usd))
            self.charges[hold.id] = hold.amount_usd
            self._active.add(hold.id)
            return hold

    async def settle(self, hold: Hold, cost_usd: float | None) -> None:
        """Replace `hold` with the trial's cost; None (unknown) keeps the full hold. An overrun never raises."""
        if cost_usd is not None and not (math.isfinite(cost_usd) and cost_usd >= 0):
            raise ValueError(f"a trial's cost must be nonnegative, finite dollars, got {cost_usd}")
        async with self._condition:
            if hold.id not in self._active:
                raise ValueError(f"hold {hold.id} is not an open reservation")
            charge = hold.amount_usd if cost_usd is None else cost_usd
            self._append(_BudgetSettle(hold=hold.id, charge_usd=charge, known=cost_usd is not None))
            self.charges[hold.id] = charge
            self._active.remove(hold.id)
            self._condition.notify_all()

    def close(self) -> None:
        self._lock.close()

    def __enter__(self) -> CampaignBudget:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
