"""JSON-line worker for the optional Python browser agent."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import sys
import time
from typing import Any


async def run(request: dict[str, object]) -> None:
    browser: Any | None = None
    try:
        browser_use = importlib.import_module("browser_use")
        agent_name = "Agent"
        browser_name = "Browser"
        agent_type = getattr(browser_use, agent_name)
        browser_type = getattr(browser_use, browser_name)
        # The host's limit for this turn, so a browser whose stop is missed stops itself (cloud.browser_minutes).
        browser = browser_type(
            keep_alive=True, cloud_timeout=request["browser_minutes"], cloud_proxy_country_code=request["proxy_country"]
        )
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
        kwargs: dict[str, object] = {"task": request["goal"], "browser": browser}
        model = request.get("model")
        if isinstance(model, str) and model:
            try:
                llm_module = importlib.import_module("browser_use.llm")
                llm_name = "ChatBrowserUse"
                kwargs["llm"] = getattr(llm_module, llm_name)(model=model)
            except ImportError:
                pass
        result = await agent_type(**kwargs).run(max_steps=1_000_000, timeout=86_400)
        served = models(result.messages)
        emit(
            {
                "event": "result",
                "status": "done" if result.is_done() else "error",
                "answer": result.final_result(),
                "error": result.error,
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
