"""Offline Jev client tests; canned replies make no quality claims."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from parallax import jev
from parallax.judging import Judgment

JEV_Q = {"done": jev.BoolQuestion(instructions=jev.Instructions(question="Is the task done?"))}
CFG = jev.JevConfig(base_url="https://fake/api/v1", api_key="test", model="typesafe/jev-1.14")


async def _evaluate(replies, state=None, questions=JEV_Q, sent=None):
    def answer(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.append(request)
        return replies.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        return await jev.evaluate(
            client,
            CFG,
            asyncio.Semaphore(1),
            lens="test",
            sample=0,
            state=state or {},
            truncated=(),
            questions=questions,
        )


async def test_jev_asks_openrouter_systemone_with_noul_questions():
    sent: list[httpx.Request] = []
    reply = {
        "model": "typesafe/jev-1.14-20261014",
        "answers": {"done": {"type": "noul", "noul": 0.94}},
        "usage": {"input_tokens": 12, "output_tokens": 3, "cost": 0.0000005},
        "id": "gen-dec-1",
        "provider": "TypeSafe",
    }
    result = await _evaluate([httpx.Response(200, json=reply)], {"x": 1}, sent=sent)
    [request] = sent
    assert request.url == "https://fake/api/v1/systemone"
    assert request.headers["authorization"] == "Bearer test"
    body = json.loads(request.content)
    assert body["model"] == "typesafe/jev-1.14" and body["questions"]["done"]["type"] == "noul"
    assert body["provider"] == {"zdr": True, "only": ["typesafe"]}
    assert result.error is None and result.answers["done"].noul == 0.94
    assert (result.input_tokens, result.cost_usd, result.generation_id) == (12, 0.0000005, "gen-dec-1")


async def test_jev_retains_each_attempt_and_usage_on_malformed_answers(monkeypatch):
    monkeypatch.setattr(jev, "BACKOFF_S", (0,))
    replies = [
        httpx.Response(503),
        httpx.Response(
            200,
            json={
                "answers": {"wrong": {"type": "noul", "noul": 0.9}},
                "usage": {"input_tokens": 7, "output_tokens": 2, "cost": 0.004},
                "id": "measured",
            },
        ),
    ]
    result = await _evaluate(replies)
    assert result.error
    assert [(a.attempt, a.cost_usd, a.input_tokens) for a in result.history] == [(1, None, None), (2, 0.004, 7)]
    assert result.history[0].error == "http 503"
    assert result.history[1].output_tokens == 2
    assert result.history[1].generation_id == "measured"


async def test_a_refused_question_settles_nothing_and_leaves_the_others():
    questions = {**JEV_Q, "other": jev.BoolQuestion(instructions=jev.Instructions(question="Is it other?"))}
    reply = {
        "answers": {"done": {"type": "refusal"}, "other": {"type": "noul", "noul": 0.02}},
        "usage": {"input_tokens": 9, "output_tokens": 1},
    }
    result = await _evaluate([httpx.Response(200, json=reply)], questions=questions)
    assert result.error is None
    assert result.refused == ("done",) and set(result.answers) == {"other"}


async def test_an_openrouter_error_reply_is_a_known_zero(monkeypatch):
    """Zero completion insurance bills no error reply (openrouter.ai/docs/guides/features/zero-completion-insurance)."""
    refused = {"error": {"code": 400, "message": "Invalid request parameters"}}
    result = await _evaluate([httpx.Response(400, json=refused)])
    assert result.error is not None and result.error.startswith("http 400")
    [attempt] = result.history
    assert (attempt.cost_usd, attempt.input_tokens) == (0.0, 0)
    assert Judgment(policy="p", state_sha256="s", questions={}, jev_call=result).cost() == (0.0, 0)

    # An unreadable body or a timeout says nothing of billing: it stays unpriced.
    monkeypatch.setattr(jev, "BACKOFF_S", ())
    result = await _evaluate([httpx.Response(500, content=b"not json")])
    assert [a.cost_usd for a in result.history] == [None]
    assert Judgment(policy="p", state_sha256="s", questions={}, jev_call=result).cost() == (0.0, 1)


@pytest.mark.parametrize(("header", "expected"), [({"retry-after": "3"}, [3.0]), ({}, None)])
async def test_a_rate_limit_waits_as_long_as_it_asks(monkeypatch, header, expected):
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(jev, "BACKOFF_S", (10.0,))
    monkeypatch.setattr(asyncio, "sleep", sleep)
    reply = {"answers": {"done": {"type": "noul", "noul": 0.5}}, "usage": {"input_tokens": 1, "output_tokens": 0}}
    await _evaluate([httpx.Response(429, headers=header), httpx.Response(200, json=reply)])
    if expected is None:
        assert len(waits) == 1 and 5.0 <= waits[0] <= 15.0  # the jittered backoff
    else:
        assert waits == expected


async def test_a_state_over_jevs_input_budget_is_never_sent():
    fits = {"text": "x" * 600_000}  # about 200k tokens at the measured floor
    reply = httpx.Response(200, json={"answers": {}, "usage": {"input_tokens": 1, "output_tokens": 0}})
    sent = await _evaluate([reply], fits)
    assert sent.attempts == 1
    too_big = {"text": "x" * (3 * jev.INPUT_TOKENS + 1)}
    skipped = await _evaluate([], too_big)  # no reply queued: any request would fail the test
    assert skipped.error is not None and skipped.error.startswith("over input budget")
    assert skipped.history == () and skipped.attempts == 0
