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
        self.cloud_cdp_url = "wss://cdp.test"
        self.id = "browser-1"
        self.live_url = "https://live.test/1"
        self.stopped = False
        FakeBrowser.instances.append(self)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True


class FakeResult:
    status = "completed"
    error = None
    usage = {"input": 10, "output": 3, "cache_read": 0, "cache_write": 0, "requests": 1}
    cost_usd = 0.012

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    def final_result(self) -> str:
        return "the answer"

    def is_successful(self) -> bool:
        return True


def fake_browser_use(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
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
            return FakeResult([message])

    module.Agent = Agent  # type: ignore[attr-defined]
    module.Browser = FakeBrowser  # type: ignore[attr-defined]
    module.ChatOpenAI = chat_openai  # type: ignore[attr-defined]
    session = ModuleType("browser_use.repl.session")
    session.MODES = ("default", "accessibility-tree-mode")  # type: ignore[attr-defined]
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
            "browser_minutes": 5,
            "proxy_country": None,
            "sensitive_data": {"shop_password": "correct-horse"},
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
    assert FakeBrowser.instances[0].kwargs == {"keep_alive": True, "cloud_timeout": 5, "cloud_proxy_country_code": None}
    assert FakeBrowser.instances[0].stopped is True
    ready, result, stopped = emitted
    assert ready["event"] == "ready" and ready["browser_id"] == "browser-1"
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
