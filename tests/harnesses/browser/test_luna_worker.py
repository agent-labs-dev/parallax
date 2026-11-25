from __future__ import annotations

import io
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from parallax.harnesses.browser import luna_worker


class FakeBrowser:
    instances: list[FakeBrowser] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.stopped = False
        self._agent_tab = None
        FakeBrowser.instances.append(self)

    def remember_tab(self) -> None:
        self._agent_tab = "tab-2"

    def _cdp(self) -> Any:
        return lambda method: {
            "targetInfos": [
                {"targetId": "tab-1", "url": "https://other.test/"},
                {"targetId": "tab-2", "url": "https://site.test/final"},
            ]
        }

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True


class FakeResult:
    status = "completed"
    error: str | None = None
    structured_output: Any = None
    usage = {"input": 10, "output": 3, "cache_read": 0, "cache_write": 0, "requests": 1}
    cost_usd = 0.012

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    def final_result(self) -> str:
        return "the answer"

    def is_successful(self) -> bool:
        return True


def fake_browser_use(monkeypatch: pytest.MonkeyPatch, result: FakeResult | None = None) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    module = ModuleType("browser_use")

    def chat_openai(model: str, **kwargs: Any) -> SimpleNamespace:
        seen["llm"] = {"model": model, **kwargs}
        return SimpleNamespace(model=model)

    class Agent:
        def __init__(self, **kwargs: Any) -> None:
            seen["agent"] = kwargs

        async def run(self, **kwargs: Any) -> FakeResult:
            seen["run"] = kwargs
            call = SimpleNamespace(
                part_kind="tool-call", tool_name="python", tool_call_id="c1", args_as_dict=lambda: {"code": "pass"}
            )
            back = SimpleNamespace(
                part_kind="tool-return",
                tool_call_id="c1",
                outcome="success",
                model_response_str=lambda: "ok",
            )
            message = SimpleNamespace(kind="response", model_name="openai/gpt-6-luna", parts=[call, back])
            outcome = result or FakeResult([])
            outcome.messages = [message]
            return outcome

    module.Agent = Agent  # type: ignore[attr-defined]
    module.Browser = FakeBrowser  # type: ignore[attr-defined]
    module.ChatOpenAI = chat_openai  # type: ignore[attr-defined]
    session = ModuleType("browser_use.repl.session")
    session.MODES = ("default", "accessibility-tree-mode")  # type: ignore[attr-defined]

    class TransientError(Exception):
        pass

    TransientError.__name__ = "ModelServerError"
    loop = ModuleType("browser_use.agent.loop")
    loop.TRANSIENT = (TransientError,)  # type: ignore[attr-defined]
    pydantic_ai = ModuleType("pydantic_ai")
    pydantic_ai.StructuredDict = lambda schema: ("structured", schema)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pydantic_ai", pydantic_ai)
    monkeypatch.setitem(sys.modules, "browser_use.agent", ModuleType("browser_use.agent"))
    monkeypatch.setitem(sys.modules, "browser_use.agent.loop", loop)
    monkeypatch.setitem(sys.modules, "browser_use", module)
    monkeypatch.setitem(sys.modules, "browser_use.repl", ModuleType("browser_use.repl"))
    monkeypatch.setitem(sys.modules, "browser_use.repl.session", session)
    return seen


async def test_the_worker_runs_the_luna_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = fake_browser_use(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}\n"))
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(luna_worker, "emit", emitted.append)

    await luna_worker.run(
        {
            "goal": "find it",
            "cdp_url": "wss://cdp.test",
            "timeout": 240.0,
            "sensitive_data": {"shop_password": "correct-horse"},
            "answer_schema": {"type": "object"},
        }
    )

    assert seen["llm"] == {
        "model": "openai/gpt-6-luna",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "or-key",
        "reasoning_effort": "low",
    }
    agent = seen["agent"]
    assert agent["task"] == "find it"
    assert agent["mode"] == "accessibility-tree-mode"
    assert agent["judge"] is False
    assert agent["sensitive_data"] == {"shop_password": "correct-horse"}
    assert agent["output_model_schema"] == ("structured", {"type": "object"})
    assert seen["run"]["timeout"] == 240.0
    assert FakeBrowser.instances[0].kwargs == {"cdp_url": "wss://cdp.test", "keep_alive": True}
    assert FakeBrowser.instances[0].stopped is True
    ready, result, stopped = emitted
    assert ready["event"] == "ready"
    assert result["url"] == "https://site.test/final"
    assert result["status"] == "completed"
    assert result["answer"] == "the answer"
    assert result["successful"] is True
    assert result["actual_models"] == ["openai/gpt-6-luna"]
    assert result["usage"] == [{**FakeResult.usage, "model": "openai/gpt-6-luna"}]
    assert result["cost_lines"] == [{"component": "model", "dollars": 0.012}]
    assert result["step_log"] == [{"tool": "python", "args": {"code": "pass"}, "output": "ok", "ok": True}]
    assert stopped == {"event": "stopped", "stopped": True}


async def test_the_worker_reports_a_missing_openrouter_key_without_starting_a_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_browser_use(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    FakeBrowser.instances.clear()
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(luna_worker, "emit", emitted.append)

    await luna_worker.run({"goal": "find it", "browser_minutes": 5, "proxy_country": None, "sensitive_data": {}})

    assert FakeBrowser.instances == []
    assert emitted[0]["status"] == "error"
    assert "OPENROUTER_API_KEY" in str(emitted[0]["error"])


async def test_the_worker_answers_with_the_structured_output_when_there_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    result = FakeResult([])
    result.structured_output = {"version": "1.2"}
    fake_browser_use(monkeypatch, result)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}\n"))
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(luna_worker, "emit", emitted.append)

    await luna_worker.run(
        {"goal": "g", "cdp_url": "wss://cdp.test", "timeout": 5.0, "answer_schema": {"type": "object"}}
    )

    assert emitted[1]["answer"] == {"version": "1.2"}


async def test_the_worker_reports_a_transient_model_failure_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    result = FakeResult([])
    result.status, result.error = "error", "ModelServerError: upstream 503"
    fake_browser_use(monkeypatch, result)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}\n"))
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(luna_worker, "emit", emitted.append)

    await luna_worker.run({"goal": "g", "cdp_url": "wss://cdp.test", "timeout": 5.0})
    other = FakeResult([])
    other.status, other.error = "error", "ValueError: bad cell"
    fake_browser_use(monkeypatch, other)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}\n"))
    await luna_worker.run({"goal": "g", "cdp_url": "wss://cdp.test", "timeout": 5.0})

    assert [e["status"] for e in emitted if e["event"] == "result"] == ["unavailable", "error"]


async def test_the_worker_still_answers_when_the_daemon_stalls_reading_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    fake_browser_use(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}\n"))
    monkeypatch.setattr(FakeBrowser, "remember_tab", lambda self: time.sleep(0.3))
    monkeypatch.setattr(luna_worker, "URL_SECONDS", 0.05)
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(luna_worker, "emit", emitted.append)

    await luna_worker.run({"goal": "g", "cdp_url": "wss://cdp.test", "timeout": 5.0})

    assert emitted[1]["url"] is None
    assert emitted[1]["answer"] == "the answer"
