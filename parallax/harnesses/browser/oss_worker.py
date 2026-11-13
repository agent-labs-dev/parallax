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
        browser = browser_type(keep_alive=True, cloud_timeout=240)
        started = time.monotonic()
        await browser.start()
        emit(
            {
                "event": "ready",
                "cdp_url": browser.cloud_cdp_url,
                "browser_id": browser.id,
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
        emit(
            {
                "event": "result",
                "status": "done" if getattr(result, "is_done", lambda: True)() else "error",
                "answer": result.final_result(),
                "step_log": [],
                "cost_lines": (
                    [{"component": "model", "dollars": result.cost_usd}] if result.cost_usd is not None else []
                ),
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


def emit(message: dict[str, object]) -> None:
    print(json.dumps(message, default=str), flush=True)


if __name__ == "__main__":
    with contextlib.redirect_stdout(sys.stdout):
        asyncio.run(run(json.loads(sys.stdin.readline())))
