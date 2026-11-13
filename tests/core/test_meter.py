"""The meter against a scripted upstream: usage and cost come from what the provider served."""

from __future__ import annotations

import asyncio
import json

import httpx
import parallax.meter as meter_module
import pytest
from parallax.meter import MeterClient, Upstream, Wire, app, llm_endpoint
from parallax.models import CostSource
from pydantic import SecretStr

ADMIN = "admin-secret"
KEY = "trial-key-1"
REAL_KEY = "sk-real-upstream"


def upstream_handler(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content) if request.content else {}
        if request.url.host == "openrouter.ai":
            model = body.get("model", "")
            if model == "fail":
                return httpx.Response(429, json={"error": {"message": "slow down"}})
            return httpx.Response(
                200,
                json={
                    "id": "c1",
                    "model": model,
                    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                    "usage": {
                        "prompt_tokens": 1000,
                        "completion_tokens": 100,
                        "prompt_tokens_details": {"cached_tokens": 200},
                        "completion_tokens_details": {"reasoning_tokens": 40},
                    },
                },
            )
        events = [
            {
                "type": "message_start",
                "message": {
                    "model": "claude-sonnet-4-5-20250929",
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 1},
                },
            },
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "message_delta", "usage": {"output_tokens": 50}},
        ]
        sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        # Split mid-line so the parser must stitch chunk boundaries.
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=ChunkedStream([sse[:37], sse[37:]])
        )

    return httpx.MockTransport(handle)


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):  # noqa: ANN204 - httpx's own signature
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
def seen() -> list[httpx.Request]:
    return []


@pytest.fixture
def meter(seen: list[httpx.Request]) -> httpx.AsyncBaseTransport:
    upstreams = {
        Wire.OPENAI: Upstream(base_url="https://openrouter.ai/api/v1", api_key=SecretStr(REAL_KEY)),
        Wire.ANTHROPIC: Upstream(base_url="https://api.anthropic.com/v1", api_key=SecretStr(REAL_KEY)),
    }
    return httpx.ASGITransport(app=app(ADMIN, upstreams, transport=upstream_handler(seen)))


async def register(meter: httpx.AsyncBaseTransport) -> MeterClient:
    control = MeterClient("http://meter", ADMIN, transport=meter)
    await control.register(KEY)
    return control


async def test_openai_json_is_metered_and_priced(meter: httpx.AsyncBaseTransport, seen: list[httpx.Request]) -> None:
    control = await register(meter)
    ep = llm_endpoint("http://meter", KEY)
    async with httpx.AsyncClient(transport=meter) as c:
        r = await c.post(
            f"{ep.openai_base_url}/chat/completions",
            headers={"authorization": f"Bearer {ep.api_key}"},
            json={"model": "openai/gpt-4o", "messages": []},
        )
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hi"
    assert seen[0].headers["authorization"] == f"Bearer {REAL_KEY}"  # trial key never reaches upstream
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    report = await control.report(KEY)
    assert report.actual_models == ("openai/gpt-4o",)
    (u,) = report.usage
    assert (u.input, u.output, u.cache_read, u.reasoning, u.requests) == (1000, 100, 200, 40, 1)
    assert u.source is CostSource.METERED
    assert u.cost_usd is not None and u.cost_usd > 0


async def test_anthropic_sse_passes_through_and_is_metered(
    meter: httpx.AsyncBaseTransport, seen: list[httpx.Request]
) -> None:
    control = await register(meter)
    ep = llm_endpoint("http://meter", KEY)
    async with httpx.AsyncClient(transport=meter) as c:
        r = await c.post(
            f"{ep.anthropic_base_url}/v1/messages",
            headers={"x-api-key": ep.api_key, "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-5", "stream": True, "messages": []},
        )
    assert r.headers["content-type"].startswith("text/event-stream")
    assert '"text": "hi"' in r.text
    assert seen[0].headers["x-api-key"] == REAL_KEY
    assert seen[0].headers["anthropic-version"] == "2023-06-01"
    # A gzip SSE body (httpx's default ask) would be unparseable for usage and relayed unlabelled.
    assert seen[0].headers["accept-encoding"] == "identity"
    report = await control.report(KEY)
    assert report.actual_models == ("claude-sonnet-4-5-20250929",)
    (u,) = report.usage
    # input is widened to include cache reads; output is the cumulative message_delta value.
    assert (u.input, u.cache_read, u.output) == (100, 90, 50)
    assert u.cost_usd is not None and u.cost_usd > 0


async def test_unknown_price_is_none_not_zero(meter: httpx.AsyncBaseTransport) -> None:
    control = await register(meter)
    ep = llm_endpoint("http://meter", KEY)
    async with httpx.AsyncClient(transport=meter) as c:
        await c.post(f"{ep.openai_base_url}/chat/completions", json={"model": "nobody/unreal-model-9", "messages": []})
    report = await control.report(KEY)
    assert report.usage[0].cost_usd is None
    assert report.unpriced_requests == 1


async def test_unknown_trial_key_and_upstream_errors(meter: httpx.AsyncBaseTransport) -> None:
    control = await register(meter)
    async with httpx.AsyncClient(transport=meter) as c:
        stranger = await c.post("http://meter/not-a-trial/openai/v1/chat/completions", json={"model": "x"})
        failed = await c.post(f"http://meter/{KEY}/openai/v1/chat/completions", json={"model": "fail"})
        wrong_admin = await c.get(f"http://meter/_meter/nope/{KEY}")
    assert stranger.status_code == 401
    assert failed.status_code == 429  # upstream status is relayed verbatim
    assert wrong_admin.status_code == 404
    report = await control.report(KEY)
    assert report.upstream_errors == 1 and report.usage == ()


async def test_streaming_chat_completions_request_usage(
    meter: httpx.AsyncBaseTransport, seen: list[httpx.Request]
) -> None:
    await register(meter)
    async with httpx.AsyncClient(transport=meter) as c:
        await c.post(
            f"http://meter/{KEY}/openai/v1/chat/completions",
            json={"model": "openai/gpt-4o", "stream": True, "stream_options": {"include_obfuscation": False}},
        )
    sent = json.loads(seen[0].content)
    assert sent["stream_options"] == {"include_obfuscation": False, "include_usage": True}


async def test_free_endpoints_are_relayed_but_not_metered(meter: httpx.AsyncBaseTransport) -> None:
    # Claude Code calls count_tokens every turn; recording it made a fully priced trial's cost unknown.
    control = await register(meter)
    ep = llm_endpoint("http://meter", KEY)
    async with httpx.AsyncClient(transport=meter) as c:
        counted = await c.post(f"{ep.anthropic_base_url}/v1/messages/count_tokens", json={"model": "claude-sonnet-4-5"})
    assert counted.status_code == 200
    report = await control.report(KEY)
    assert report.usage == () and report.unpriced_requests == 0


async def test_non_inference_endpoints_never_reach_upstream(
    meter: httpx.AsyncBaseTransport, seen: list[httpx.Request]
) -> None:
    # The upstream key can read, write and delete account files; only inference may borrow it.
    control = await register(meter)
    ep = llm_endpoint("http://meter", KEY)
    async with httpx.AsyncClient(transport=meter) as c:
        refused = [
            await c.get(f"{ep.openai_base_url}/files"),
            await c.get(f"{ep.openai_base_url}/files/private-file/content"),
            await c.delete(f"{ep.openai_base_url}/files/private-file"),
            await c.get(f"{ep.openai_base_url}/responses/resp_1"),
            await c.post(f"{ep.openai_base_url}/fine_tuning/jobs", json={}),
            # Allowed endpoint, but a background response spends after the request returns, unmetered.
            await c.post(f"{ep.openai_base_url}/responses", json={"model": "m", "input": "hi", "background": True}),
            await c.post(f"{ep.anthropic_base_url}/v1/messages/batches", json={}),
            await c.get(f"{ep.anthropic_base_url}/v1/messages"),
        ]
    assert [r.status_code for r in refused] == [403] * len(refused)
    assert refused[0].json()["error"]["code"] == "endpoint_not_allowed"
    assert refused[-1].json()["error"]["type"] == "permission_error"
    assert seen == []
    report = await control.report(KEY)
    assert report.usage == () and report.upstream_errors == 0


async def test_server_side_tools_are_refused_before_upstream(
    meter: httpx.AsyncBaseTransport, seen: list[httpx.Request]
) -> None:
    # A provider-run web search or fetch would read the live web from outside the box's isolation.
    await register(meter)
    ep = llm_endpoint("http://meter", KEY)
    fn = {"type": "function", "name": "f", "parameters": {}}
    async with httpx.AsyncClient(transport=meter) as c:
        anthropic = f"{ep.anthropic_base_url}/v1/messages"
        responses = f"{ep.openai_base_url}/responses"
        chat = f"{ep.openai_base_url}/chat/completions"
        refused = [
            await c.post(anthropic, json={"model": "m", "tools": [{"type": "web_search_20250305", "name": "ws"}]}),
            await c.post(anthropic, json={"model": "m", "mcp_servers": [{"url": "https://x"}]}),
            await c.post(responses, json={"model": "m", "tools": [fn, {"type": "web_search"}]}),
            # A namespace groups client tools, so a server tool hidden inside one is still found.
            await c.post(responses, json={"model": "m", "tools": [{"type": "namespace", "tools": [{"type": "mcp"}]}]}),
            # Only Anthropic dates tool types, so a dated namespace is not a namespace.
            await c.post(responses, json={"model": "m", "tools": [{"type": "namespace_20250101", "tools": []}]}),
            await c.post(chat, json={"model": "m", "plugins": [{"id": "web"}]}),
            await c.post(chat, json={"model": "m", "web_search_options": {}}),
            await c.post(chat, json={"model": "openai/gpt-4o:online"}),
        ]
        unreadable = await c.post(anthropic, content=b"\x1f\x8b gzip", headers={"content-encoding": "gzip"})
        # The shapes harnesses send: untyped Anthropic tools, function + namespace.
        allowed = [
            await c.post(anthropic, json={"model": "claude-sonnet-4-5", "tools": [{"name": "Bash"}]}),
            await c.post(chat, json={"model": "openai/gpt-4o", "tools": [fn], "plugins": []}),
            await c.post(chat, json={"model": "openai/gpt-4o", "tools": [{"type": "namespace", "tools": [fn]}]}),
            # A parser that keeps the first duplicate key would see the web search the meter never did.
            await c.post(anthropic, content=b'{"model": "m", "tools": [{"type": "web_search_20250305"}], "tools": []}'),
        ]
    assert [r.status_code for r in refused] == [403] * len(refused)
    assert refused[0].json()["error"] == {
        "type": "permission_error",
        "message": "parallax meter: provider-side tools are not allowed (trials have no live web access): "
        "tool type web_search_20250305",
    }
    assert refused[2].json()["error"]["code"] == "server_tool_not_allowed"
    assert unreadable.status_code == 400
    assert [r.status_code for r in allowed] == [200] * len(allowed)
    assert len(seen) == len(allowed)
    assert b"web_search" not in seen[-1].content


async def test_abandoned_stream_is_read_to_its_end_and_priced() -> None:
    """A harness killed mid-stream (killed at its timeout) left usage unread and the trial unpriced."""
    seen: list[httpx.Request] = []
    finish = asyncio.Event()
    usage = {"input_tokens": 5000, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 3000}}
    usage["input_tokens_details"]["cache_write_tokens"] = 1500
    head = {"type": "response.output_text.delta", "delta": "hi"}
    tail = {"type": "response.completed", "response": {"model": "anthropic/claude-sonnet-4.5", "usage": usage}}

    class Held(httpx.AsyncByteStream):
        async def __aiter__(self):  # noqa: ANN204 - httpx's own signature
            yield f"data: {json.dumps(head)}\n\n".encode()
            await finish.wait()
            yield f"data: {json.dumps(tail)}\n\n".encode()

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Held())

    upstreams = {w: Upstream(base_url="https://openrouter.ai/api/v1", api_key=SecretStr(REAL_KEY)) for w in Wire}
    meter = httpx.ASGITransport(app=app(ADMIN, upstreams, transport=httpx.MockTransport(handle)))
    control = await register(meter)
    async with httpx.AsyncClient(transport=meter) as c:
        body = {"model": "anthropic/claude-sonnet-4.5", "stream": True, "input": []}
        post = asyncio.create_task(c.post(f"http://meter/{KEY}/openai/v1/responses", json=body))
        while not seen:
            await asyncio.sleep(0.01)
        post.cancel()  # the client hangs up before the usage event
        finish.set()
    report = await control.report(KEY)
    (u,) = report.usage
    assert (u.input, u.cache_read, u.cache_write, u.output, u.requests) == (5000, 3000, 1500, 20, 1)
    assert u.cost_usd is not None and report.unpriced_requests == 0


async def test_stream_open_past_the_drain_wait_is_unpriced_and_final(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream still open at report time is counted as unknown cost; its late usage cannot rewrite the report."""
    monkeypatch.setattr(meter_module, "STREAM_DRAIN_WAIT_S", 0.05)
    started, finish = asyncio.Event(), asyncio.Event()
    tail = {"type": "response.completed", "response": {"model": "anthropic/claude-sonnet-4.5", "usage": {}}}
    tail["response"]["usage"] = {"input_tokens": 5000, "output_tokens": 20}

    class Held(httpx.AsyncByteStream):
        async def __aiter__(self):  # noqa: ANN204 - httpx's own signature
            started.set()
            await finish.wait()
            yield f"data: {json.dumps(tail)}\n\n".encode()

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Held())

    upstreams = {w: Upstream(base_url="https://openrouter.ai/api/v1", api_key=SecretStr(REAL_KEY)) for w in Wire}
    meter = httpx.ASGITransport(app=app(ADMIN, upstreams, transport=httpx.MockTransport(handle)))
    control = await register(meter)
    async with httpx.AsyncClient(transport=meter) as c:
        body = {"model": "anthropic/claude-sonnet-4.5", "stream": True, "input": []}
        post = asyncio.create_task(c.post(f"http://meter/{KEY}/openai/v1/responses", json=body))
        await started.wait()
        post.cancel()
    served = await control.report(KEY)
    finish.set()
    await asyncio.sleep(0.05)
    for report in (served, await control.report(KEY)):
        (u,) = report.usage
        assert (u.requests, u.input, u.cost_usd, report.unpriced_requests) == (1, 0, None, 1)


# ---------------------------------------------------------------------------
# campaign budget
# ---------------------------------------------------------------------------


async def test_the_budget_admits_trials_while_their_reservations_fit_and_settles_their_cost(tmp_path) -> None:
    from parallax.meter import BudgetRefused, CampaignBudget

    journal = tmp_path / "budget.jsonl"
    with CampaignBudget(1.0, 0.4, journal) as budget:
        first = await budget.reserve("a")
        second = await budget.reserve("b")
        assert budget.committed == pytest.approx(0.8)
        await budget.settle(first, 0.1)  # the cost replaces the hold, freeing room
        third = await budget.reserve("c")
        await budget.settle(second, None)  # unknown: the full hold stays committed
        await budget.settle(third, 0.5)  # an overrun is recorded as it is and stops nothing
        assert budget.committed == pytest.approx(1.0)
        with pytest.raises(BudgetRefused, match="does not fit"):
            await budget.reserve("d")
    events = [json.loads(line)["event"] for line in journal.read_text().splitlines()]
    assert events == ["header", "reserve", "reserve", "settle", "reserve", "settle", "settle"]


async def test_a_resumed_budget_keeps_an_interrupted_hold_and_refuses_other_limits(tmp_path) -> None:
    from parallax.meter import BudgetRefused, CampaignBudget

    journal = tmp_path / "budget.jsonl"
    with CampaignBudget(1.0, 0.6, journal) as budget:
        await budget.reserve("interrupted")  # never settled: the process died inside the trial
    with CampaignBudget(1.0, 0.6, journal) as resumed:
        assert resumed.committed == pytest.approx(0.6)
        with pytest.raises(BudgetRefused):
            await resumed.reserve("next")
    with pytest.raises(ValueError, match="same"):
        CampaignBudget(2.0, 0.6, journal)


async def test_a_reservation_waits_for_a_trial_in_flight_to_settle(tmp_path) -> None:
    from parallax.meter import CampaignBudget

    with CampaignBudget(1.0, 0.6, tmp_path / "budget.jsonl") as budget:
        running = await budget.reserve("a")
        waiting = asyncio.create_task(budget.reserve("b"))
        await asyncio.sleep(0)
        assert not waiting.done()
        await budget.settle(running, 0.2)
        assert (await waiting).trial == "b"


def test_one_process_owns_a_budget_journal(tmp_path) -> None:
    from parallax.meter import CampaignBudget

    with CampaignBudget(1.0, 0.5, tmp_path / "budget.jsonl"), pytest.raises(ValueError, match="another process"):
        CampaignBudget(1.0, 0.5, tmp_path / "budget.jsonl")
