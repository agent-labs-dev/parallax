"""JSON-line worker for browser-use 1.x in Browser Use's fast gateway configuration.

The model is fixed by the arm, not the trial: GPT-6 Luna through OpenRouter at
low reasoning effort, run in browser-use's accessibility-tree mode. OpenRouter serves it
under the OpenAI-compatible id `openai/gpt-6-luna`, and `ChatOpenAI` with a
non-OpenAI `base_url` sends chat completions with `reasoning_effort` as-is, so
the effort goes on the model, not `Agent(reasoning=...)`, which such servers do
not receive. Parallax judges every run itself, so browser-use's own judge,
which defaults to on and spends a model call, is off.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import sys
import time
from typing import Any

MODEL = "openai/gpt-6-luna"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


async def run(request: dict[str, object]) -> None:
    browser: Any | None = None
    try:
        browser_use = importlib.import_module("browser_use")
        agent_name = "Agent"
        browser_name = "Browser"
        llm_name = "ChatOpenAI"
        agent_type = getattr(browser_use, agent_name)
        browser_type = getattr(browser_use, browser_name)
        chat_openai = getattr(browser_use, llm_name)
        # The accessibility-tree mode is the library's one non-default mode; its name comes from the library's own list.
        agent_mode = next(m for m in importlib.import_module("browser_use.repl.session").MODES if m != "default")
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("browser-use-1-luna needs OPENROUTER_API_KEY for its model")
        # The host's limit for this turn, so a browser whose stop is missed stops itself (cloud.browser_minutes).
        browser = browser_type(keep_alive=True, cloud_timeout=request["browser_minutes"])
        started = time.monotonic()
        await browser.start()
        emit(
            {
                "event": "ready",
                "cdp_url": browser.cloud_cdp_url,
                "browser_id": browser.id,
                "live_url": browser.live_url,
                "open_seconds": time.monotonic() - started,
            }
        )
        llm = chat_openai(MODEL, base_url=OPENROUTER_BASE_URL, api_key=key, reasoning_effort="low")
        sensitive = request.get("sensitive_data")
        agent = agent_type(
            task=request["goal"],
            llm=llm,
            browser=browser,
            mode=agent_mode,
            judge=False,
            sensitive_data=sensitive if isinstance(sensitive, dict) and sensitive else None,
        )
        result = await agent.run(max_steps=1_000_000, timeout=86_400)
        served = models(result.messages)
        emit(
            {
                "event": "result",
                "status": result.status,
                "answer": result.final_result(),
                "error": result.error,
                "successful": result.is_successful(),
                "step_log": steps(result.messages),
                "actual_models": served,
                "usage": [{**result.usage, "model": served[0]}] if len(served) == 1 else [],
                # None when browser-use has no price for the model: unpriced, never free.
                "cost_lines": [{"component": "model", "dollars": result.cost_usd}],
            }
        )
        await asyncio.to_thread(sys.stdin.readline)
    except Exception as error:
        emit({"event": "result", "status": "error", "answer": None, "error": f"{type(error).__name__}: {error}"})
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.stop()
        emit({"event": "stopped", "stopped": True})


def steps(messages: list[Any]) -> list[dict[str, object]]:
    """Each tool call the agent made, with what it returned. browser-use 1.x keeps no per-step history: its agent
    runs Python cells, and the run's transcript (Pydantic AI messages) is the record of them."""
    calls: dict[str, dict[str, object]] = {}
    log: list[dict[str, object]] = []
    for message in messages:
        for part in message.parts:
            match part.part_kind:
                case "tool-call":
                    step: dict[str, object] = {"tool": part.tool_name, "args": part.args_as_dict()}
                    calls[part.tool_call_id] = step
                    log.append(step)
                case "tool-return" | "retry-prompt" if part.tool_call_id in calls:
                    step = calls[part.tool_call_id]
                    if part.part_kind == "tool-return":
                        step["output"], step["ok"] = part.model_response_str(), part.outcome == "success"
                    else:
                        step["output"], step["ok"] = part.model_response(), False
                case _:
                    pass
    return log


def models(messages: list[Any]) -> list[str]:
    """The models that answered, as their responses name them."""
    return list(dict.fromkeys(m.model_name for m in messages if m.kind == "response" and m.model_name))


def emit(message: dict[str, object]) -> None:
    print(json.dumps(message, default=str), flush=True)


if __name__ == "__main__":
    with contextlib.redirect_stdout(sys.stdout):
        asyncio.run(run(json.loads(sys.stdin.readline())))
