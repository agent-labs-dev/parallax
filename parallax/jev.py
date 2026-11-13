"""TypeSafe's Jev evaluator over OpenRouter's `/api/v1/systemone` endpoint (see docs/jev-judge.md).

Jev answers typed `noul` questions about a JSON state with probabilities and no text. This module owns
transport and strict reply validation. `parallax.semantic` uses it for grading, with an LLM fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import time
from collections.abc import Mapping
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from parallax.judge import OPENROUTER_BASE_URL, OPENROUTER_KEY_ENV, JudgeConfigError
from parallax.models import Model

KEY_ENV = OPENROUTER_KEY_ENV
BASE_URL_ENV = "PARALLAX_JEV_BASE_URL"
MODEL_ENV = "PARALLAX_JEV_MODEL"
DEFAULT_BASE_URL = OPENROUTER_BASE_URL
DEFAULT_MODEL = "typesafe/jev-1.14"
TIMEOUT_S = 120.0
"""The fallback panel's bound too. A state near `INPUT_TOKENS` runs in TypeSafe's long-context mode, which TypeSafe
documents as slower (docs.typesafe.ai/models), so the old 15 s cut off states Jev would have answered."""
BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
"""Jittered exponential backoff between attempts, unless a 429 names its own wait in `Retry-After`."""
RETRY_STATUS = frozenset({429, 500, 502, 503, 504, 524, 529})
"""OpenRouter's transient failures: rate limit, server and provider errors, edge timeout (524), provider overload
(529); see the error responses of openrouter.ai/docs/api/api-reference/systemone."""
TOKENS_PER_BYTE = 0.25  # rough, for the state cap only; OpenRouter reports the billed count
INPUT_TOKENS = 262_144
"""Jev's input limit on this route: the state plus every question (OpenRouter's catalog `context_length` and
`max_prompt_tokens` for typesafe/jev-1.14 and ~typesafe/jev-latest, and its Jev guide, checked 2026-11-09). OpenRouter
runs a request over about 32,000 bytes in TypeSafe's long-context mode on its own, so no long-context header is sent;
without that mode TypeSafe's own limits are 64k per request and 32k per question (docs.typesafe.ai/models)."""
MIN_TOKENS_PER_BYTE = 1 / 3
"""A floor on Jev's tokens per byte of JSON, for skipping a state that cannot fit. On 2026-10-16's 108 judged browser
states over 20 KB the billed count ran 0.37-0.59 tokens per byte; a state that fails this floor would bill at least
`INPUT_TOKENS`, so it is never sent. One that passes may still be refused, which an error reply's known $0 covers."""


class JevConfig(Model):
    base_url: str
    api_key: str
    model: str


def config_from_env() -> JevConfig:
    key = os.environ.get(KEY_ENV)
    if not key:
        raise JudgeConfigError(f"Jev needs ${KEY_ENV} (an OpenRouter key)")
    return JevConfig(
        base_url=os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL),
        api_key=key,
        model=os.environ.get(MODEL_ENV, DEFAULT_MODEL),
    )


# ---------------------------------------------------------------------------
# questions (wire shape: docs.typesafe.ai primitives)
# ---------------------------------------------------------------------------


class Option(Model):
    """A criterion: what choosing it means, its contrastive boundary, and one example."""

    what: str
    not_for: str | None = None
    examples: tuple[str, ...] | None = None


class Instructions(Model):
    question: str
    inspect: tuple[str, ...] | None = None  # state fields the question is about


class BoolCriteria(Model):
    true: Option
    false: Option


class BoolQuestion(Model):
    type: Literal["noul"] = "noul"
    instructions: Instructions
    criteria: BoolCriteria | None = None


# ---------------------------------------------------------------------------
# replies
# ---------------------------------------------------------------------------


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class NoulAnswer(_Wire):
    type: Literal["noul"]
    noul: Probability


class _Refusal(_Wire):
    """Jev declined this question; the others in the request can still be answered."""

    type: Literal["refusal"]


class _Usage(_Wire):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost: float | None = None


class _UsageEnvelope(_Wire):
    id: str | None = None
    usage: _Usage | None = None


class _Reply(_Wire):
    answers: dict[str, Annotated[NoulAnswer | _Refusal, Field(discriminator="type")]]
    usage: _Usage


class JevAttempt(Model):
    """A provider attempt; absent usage remains unknown, including failed requests."""

    attempt: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    generation_id: str | None = None
    latency_ms: int
    error: str | None = None


class JevCall(Model):
    """One evaluate call, answered or not."""

    lens: str
    sample: int
    state_sha256: str
    state_tokens: int  # estimated from bytes before sending
    truncated: tuple[str, ...]
    answers: dict[str, NoulAnswer] = {}
    refused: tuple[str, ...] = ()  # questions Jev declined; they settle nothing
    error: str | None = None  # set => nothing here may settle a label
    input_tokens: int = 0
    cost_usd: float | None = None
    generation_id: str | None = None
    latency_ms: int = 0
    attempts: int = 0
    history: tuple[JevAttempt, ...] = ()


def state_sha(state: Mapping[str, JsonValue]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


async def evaluate(
    client: httpx.AsyncClient,
    cfg: JevConfig,
    gate: asyncio.Semaphore,
    *,
    lens: str,
    sample: int,
    state: Mapping[str, JsonValue],
    truncated: tuple[str, ...],
    questions: Mapping[str, BoolQuestion],
) -> JevCall:
    """Ask `questions` about `state`; never raises for a provider problem, it records `error`."""
    body = {
        "model": cfg.model,
        "state": state,
        "questions": {n: q.model_dump(mode="json", exclude_none=True) for n, q in questions.items()},
        "provider": {"zdr": True, "only": ["typesafe"]},
    }
    raw = json.dumps(state, sort_keys=True)
    base = JevCall(
        lens=lens, sample=sample, state_sha256=state_sha(state),
        state_tokens=int(len(raw.encode()) * TOKENS_PER_BYTE), truncated=truncated,
    )  # fmt: skip
    if (over := over_budget(raw, questions)) is not None:
        return base.model_copy(update={"error": over})
    errors: list[str] = []
    history: list[JevAttempt] = []
    started = time.monotonic()
    wait: float | None = None
    for attempt in range(len(BACKOFF_S) + 1):
        if attempt:
            await asyncio.sleep(wait if wait is not None else BACKOFF_S[attempt - 1] * random.uniform(0.5, 1.5))
            wait = None
        t0 = time.monotonic()
        try:
            async with gate:
                t0 = time.monotonic()
                resp = await client.post(
                    f"{cfg.base_url.rstrip('/')}/systemone",
                    json=body,
                    headers={"Authorization": f"Bearer {cfg.api_key}"},
                    timeout=TIMEOUT_S,
                )
                latency = int((time.monotonic() - t0) * 1000)
        except httpx.TimeoutException:
            errors.append("timeout")
            history.append(
                JevAttempt(attempt=attempt + 1, latency_ms=int((time.monotonic() - t0) * 1000), error="timeout")
            )
            continue
        except httpx.HTTPError as exc:
            errors.append(type(exc).__name__)
            history.append(
                JevAttempt(
                    attempt=attempt + 1, latency_ms=int((time.monotonic() - t0) * 1000), error=type(exc).__name__
                )
            )
            continue
        parsed = _parsed(base, resp.content, questions, latency, attempt + 1)
        entry = parsed.history[0]
        if resp.status_code != httpx.codes.OK:
            entry = entry.model_copy(update={"error": f"http {resp.status_code}"})
            if entry.cost_usd is None and refused(resp.content):
                entry = entry.model_copy(update={"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        history.append(entry)
        if resp.status_code in RETRY_STATUS:
            errors.append(f"http {resp.status_code}")
            if resp.status_code == httpx.codes.TOO_MANY_REQUESTS:
                wait = retry_after(resp)
            continue
        if resp.status_code != httpx.codes.OK:
            return base.model_copy(update={"error": f"http {resp.status_code}: {resp.text[:200]}",
                                           "cost_usd": entry.cost_usd, "generation_id": entry.generation_id,
                                           "attempts": attempt + 1, "history": tuple(history)})  # fmt: skip
        return parsed.model_copy(update={"history": tuple(history)})
    total = int((time.monotonic() - started) * 1000)
    return base.model_copy(
        update={"error": " | ".join(errors), "attempts": len(errors), "latency_ms": total, "history": tuple(history)}
    )


def over_budget(raw_state: str, questions: Mapping[str, BoolQuestion]) -> str | None:
    """Why a state cannot fit Jev's input, or None. Measured as the route counts it: the state plus every question."""
    asked = sum(len(q.model_dump_json(exclude_none=True).encode()) for q in questions.values())
    floor = int((len(raw_state.encode()) + asked) * MIN_TOKENS_PER_BYTE)
    if floor <= INPUT_TOKENS:
        return None
    return f"over input budget: at least {floor} tokens, Jev accepts {INPUT_TOKENS}; not sent"


class _ErrorData(_Wire):
    code: int
    message: str


class _ErrorEnvelope(_Wire):
    error: _ErrorData


def refused(content: bytes) -> bool:
    """Whether `content` is OpenRouter's error reply (`{"error": {"code", "message"}}`). Such a reply has no output
    tokens, so zero completion insurance bills it nothing, on every account and model
    (openrouter.ai/docs/guides/features/zero-completion-insurance). A timeout or an unreadable body stays unpriced."""
    try:
        _ErrorEnvelope.model_validate_json(content)
    except ValidationError:
        return False
    return True


def retry_after(resp: httpx.Response) -> float | None:
    """A 429's `Retry-After` in seconds, which TypeSafe's own clients follow (docs.typesafe.ai/models), or None."""
    try:
        return max(0.0, float(resp.headers["retry-after"]))
    except (KeyError, ValueError):
        return None


def _parsed(
    base: JevCall, content: bytes, questions: Mapping[str, BoolQuestion], latency: int, attempts: int
) -> JevCall:
    envelope: _UsageEnvelope | None = None
    with contextlib.suppress(ValidationError):
        envelope = _UsageEnvelope.model_validate_json(content)
    usage = envelope.usage if envelope else None
    entry = JevAttempt(
        attempt=attempts,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        cost_usd=usage.cost if usage else None,
        generation_id=envelope.id if envelope else None,
        latency_ms=latency,
    )
    update: dict[str, object] = {
        "input_tokens": usage.input_tokens if usage else 0,
        "cost_usd": entry.cost_usd,
        "generation_id": entry.generation_id,
        "latency_ms": latency,
        "attempts": attempts,
    }
    try:
        wire = _Reply.model_validate_json(content)
        # Exactly the questions asked; the reply model already rejects any answer that is neither noul nor a refusal.
        match = set(wire.answers) == set(questions)
        error = None if match else f"answers do not match the questions asked: {sorted(wire.answers)}"
        if error is None:
            update["answers"] = {k: a for k, a in wire.answers.items() if isinstance(a, NoulAnswer)}
            update["refused"] = tuple(k for k, a in wire.answers.items() if isinstance(a, _Refusal))
    except ValidationError as exc:
        error = f"malformed: {exc.error_count()} errors"
    update["error"] = error
    update["history"] = (entry.model_copy(update={"error": error}),)
    return base.model_copy(update=update)
