"""Browser harness implementations.

The classes deliberately keep provider SDK imports inside the execution methods.
Importing Parallax, listing harnesses, and running offline tests therefore never
requires browser credentials or a third-party browser package.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import shlex
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from parallax.harnesses.base import config_hash
from parallax.harnesses.browser.clock import SESSION_INCLUDED, AttemptClock
from parallax.harnesses.browser.cloud import (
    Browser,
    _failure,
    _settled,
    _stop,
    cloud_browser,
    cost_lines,
    devtools_websocket,
)
from parallax.harnesses.browser.observer import load_observer
from parallax.harnesses.browser.site import Unavailable, site_down
from parallax.harnesses.browser.subprocess import JsonWorker
from parallax.models import (
    Area,
    Capability,
    CostSource,
    HarnessInfo,
    HarnessKind,
    Isolation,
    Step,
    StepKind,
    Task,
    Timing,
    Trial,
    TurnInput,
    TurnOutput,
    TurnStatus,
    Usage,
)

logger = logging.getLogger(__name__)


class BrowserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    harness: str
    observer: str = "plain-cdp"
    cloud_api: str = "v4"
    poll_seconds: float = 1.0


def _browser_capability() -> frozenset[Capability]:
    capability = getattr(Capability, "BROWSER", None)
    return frozenset({capability}) if isinstance(capability, Capability) else frozenset()


def _info(
    name: str,
    *,
    pins_model: bool,
    credential_env: tuple[str, ...] = (),
    install: str = "",
    isolation: Isolation = Isolation.DOCKER,
) -> HarnessInfo:
    return HarnessInfo(
        name=name,
        kind=HarnessKind.OPEN,
        capabilities=_browser_capability(),
        pins_model=pins_model,
        cost_source=CostSource.HARNESS,
        isolation=isolation,
        install=install,
        credential_env=credential_env,
        source_url="https://parallax.invalid/browser-harness",
    )


def _step(turn: int, raw: Mapping[str, Any]) -> Step:
    kind = raw.get("kind", "tool_call")
    try:
        parsed_kind = StepKind(kind)
    except ValueError:
        parsed_kind = StepKind.TOOL_CALL
    return Step(
        turn=turn,
        kind=parsed_kind,
        name=raw.get("name") or raw.get("operation") or raw.get("tool"),
        args=raw.get("args", raw.get("target", raw.get("arguments"))),
        output=raw.get("output", raw.get("observation")),
        ok=raw.get("ok"),
        t_ms=int(raw.get("t_ms", 0)),
    )


def _steps(turn: int, raw: object) -> tuple[Step, ...]:
    return tuple(_step(turn, item) for item in raw if isinstance(item, Mapping)) if isinstance(raw, list) else ()


def _mapping_list(raw: object) -> list[dict[str, Any]]:
    return [dict(item) for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _usage(raw: object, model: str | None) -> tuple[Usage, ...]:
    if not isinstance(raw, list):
        return ()
    output: list[Usage] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            output.append(
                Usage(
                    model=str(item.get("model") or model or "unreported"),
                    input=int(item.get("input", item.get("input_tokens", 0))),
                    output=int(item.get("output", item.get("output_tokens", 0))),
                    cache_read=int(item.get("cache_read", item.get("cached_input_tokens", 0))),
                    reasoning=int(item.get("reasoning", 0)),
                    requests=int(item.get("requests", 1)),
                    cost_usd=float(item["cost_usd"]) if item.get("cost_usd") is not None else None,
                    source=CostSource.HARNESS,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(output)


def _normalise_status(raw: object) -> TurnStatus:
    return TurnStatus.COMPLETED if str(raw) in {"done", "completed", "stopped"} else TurnStatus.HARNESS_ERROR


def _interruption(raw: Mapping[str, Any]) -> str | None:
    error = raw.get("error")
    if not isinstance(error, str):
        return None
    if error.startswith("environment interruption: "):
        return error
    if str(raw.get("status")) == "unavailable" or raw.get("site_down"):
        return f"environment interruption: {error}"
    return None


def _evidence_payload(
    *,
    page: Mapping[str, Any] | None,
    answer: str | None,
    steps: object,
    citations: object = (),
) -> dict[str, Any]:
    final = None if page is None else dict(page)
    if final is not None:
        final = {key: final[key] for key in ("url", "title", "text", "screenshot_png_b64", "controls") if key in final}
        final.setdefault("url", "")
        final.setdefault("title", "")
        final.setdefault("text", "")
    final_url = "" if final is None else str(final.get("url", ""))
    timeline = []
    if isinstance(steps, list):
        for item in steps:
            if not isinstance(item, Mapping):
                continue
            action = item.get("action") or item.get("operation") or item.get("tool")
            if not isinstance(action, str):
                continue
            step = {"url": str(item.get("url") or final_url), "action": action}
            for key in ("value", "quote"):
                if isinstance(item.get(key), str):
                    step[key] = item[key]
            timeline.append(step)
    payload = {
        "final_page": final,
        "answer": answer,
        "steps": timeline,
        "citations": [dict(item) for item in citations if isinstance(item, Mapping)]
        if isinstance(citations, list)
        else [],
    }
    try:
        from parallax.browser_evidence import BrowserEvidence
    except ImportError:
        return payload
    evidence = BrowserEvidence.model_validate(payload)
    return evidence.model_dump(mode="json", exclude_none=True)


class BrowserHarness(ABC):
    """Common Parallax protocol and evidence hooks for a browser runner."""

    info: HarnessInfo

    def __init__(self, *, observer: str | None = None) -> None:
        self.config = BrowserConfig(
            harness=self.info.name,
            observer=observer or os.environ.get("PARALLAX_BROWSER_OBSERVER", "plain-cdp"),
            cloud_api=os.environ.get("PARALLAX_BROWSER_CLOUD_API", "v4"),
            poll_seconds=float(os.environ.get("PARALLAX_BROWSER_POLL_SECONDS", "1")),
        )
        self._trial: Trial | None = None
        self._clock: AttemptClock | None = None
        self._browser: Browser | None = None
        self._answer: str | None = None
        self._page: dict[str, Any] | None = None
        self._steps: list[dict[str, Any]] = []
        self._citations: list[dict[str, Any]] = []
        self._cost: dict[str, float | None] = {}
        self._details: dict[str, Any] = {"observer": self.config.observer}
        self._start_url: str | None = None
        self._site_preflight_done = False
        self.trial_invalid: list[str] = []
        self.trial_notes: list[str] = []

    @property
    def config_hash(self) -> str:
        return config_hash(self.config)

    @property
    def trial(self) -> Trial:
        if self._trial is None:
            raise RuntimeError("browser harness has not started")
        return self._trial

    def bind_task(self, task: Task) -> None:
        """Remember a source task's start URL so a site outage never becomes an agent failure."""
        if task.meta.area is Area.BROWSER:
            value = task.data.get("website", task.data.get("start_url"))
            self._start_url = value if isinstance(value, str) else None

    async def _site_preflight(self) -> str | None:
        if self._site_preflight_done or self._start_url is None:
            return None
        self._site_preflight_done = True
        async with httpx.AsyncClient() as client:
            return await site_down(client, self._start_url)

    async def start(self, trial: Trial, box: Any) -> None:
        self._trial = trial
        self._clock = AttemptClock(bounded=False)
        self._details["task_id"] = trial.task_id
        self._details["model"] = trial.model

    async def turn(self, turn: TurnInput) -> TurnOutput:
        began = time.monotonic()
        try:
            reason = await self._site_preflight()
            if reason is not None:
                result = {"status": "unavailable", "answer": None, "error": reason}
            else:
                self._agent_started()
                result = await self.execute(turn)
        except Unavailable as error:
            result = {"status": "unavailable", "answer": None, "error": str(error)}
        except Exception as error:
            result = {"status": "error", "answer": None, "error": f"{type(error).__name__}: {error}"}
        status = _normalise_status(result.get("status"))
        answer = result.get("answer")
        self._answer = answer if isinstance(answer, str) else None
        self._steps.extend(_mapping_list(result.get("step_log")))
        self._citations.extend(_mapping_list(result.get("citations")))
        if self._clock is not None and status is TurnStatus.COMPLETED:
            self._clock.answered()
        page = result.get("final_page")
        if isinstance(page, Mapping):
            self._page = dict(page)
        self._cost.update(_known_costs(result.get("cost_lines")))
        self._details.update(
            {
                "status": str(result.get("status", "error")),
                "error": result.get("error"),
                "steps": len(self._steps),
                "wall_seconds": round(time.monotonic() - began, 2),
            }
        )
        interruption = _interruption(result)
        error = interruption or (str(result["error"]) if result.get("error") is not None else None)
        latency = result.get("latency_ms")
        latency_ms = int(float(latency)) if isinstance(latency, int | float) else None
        return TurnOutput(
            status=status,
            answer=self._answer or "",
            session_id=result.get("session_id"),
            steps=_steps(turn.index, result.get("step_log")),
            usage=_usage(result.get("usage"), self.trial.model),
            actual_model=result.get("actual_model") if isinstance(result.get("actual_model"), str) else None,
            latency_ms=latency_ms,
            error=error,
        )

    async def stop(self) -> None:
        if self._clock is not None:
            self._clock.closed_at()
        await self.cleanup()

    async def version(self, box: Any) -> str:
        return self.info.name

    @property
    def trial_metadata(self) -> dict[str, Any]:
        return {
            "browser": _evidence_payload(
                page=self._page, answer=self._answer, steps=self._steps, citations=self._citations
            )
        }

    def trial_timing(self) -> Timing:
        return self._clock.timing() if self._clock is not None else Timing(basis=SESSION_INCLUDED, breakdown={})

    def trial_costs(self) -> dict[str, float | None]:
        return dict(self._cost)

    def trial_details(self) -> dict[str, Any]:
        return dict(self._details)

    @abstractmethod
    async def execute(self, turn: TurnInput) -> dict[str, Any]: ...

    async def cleanup(self) -> None:
        return None

    async def observe(self, cdp_url: str, *, url: str | None = None) -> dict[str, Any]:
        return await load_observer(self.config.observer).observe(cdp_url, url=url)

    def _agent_started(self, open_seconds: object = None) -> None:
        if self._clock is None:
            return
        if isinstance(open_seconds, int | float):
            self._clock.session_opened(float(open_seconds))
        self._clock.ready()


def _known_costs(raw: object) -> dict[str, float | None]:
    if not isinstance(raw, list):
        return {}
    # A component may be billed in several lines (one per call or purpose): its cost is their sum, and unknown when
    # any line's amount is.
    output: dict[str, float | None] = {}
    for item in raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("component"), str):
            continue
        component = str(item["component"])
        try:
            dollars = float(item["dollars"]) if item.get("dollars") is not None else None
        except (TypeError, ValueError):
            # An unreadable amount is still a charge: unknown, never zero or absent.
            dollars = None
        before = output.get(component, 0.0)
        output[component] = None if dollars is None or before is None else before + dollars
    return output


class BrowserHarnessCloud(BrowserHarness):
    info = _info("browser-use-cloud", pins_model=False, credential_env=("BROWSER_USE_API_KEY",))

    async def execute(self, turn: TurnInput) -> dict[str, Any]:
        client_module = importlib.import_module(f"browser_use_sdk.{self.config.cloud_api}")
        client_name = "AsyncBrowserUse"
        client_cls = getattr(client_module, client_name)
        client = client_cls(api_key=self.trial.env.get("BROWSER_USE_API_KEY") or os.environ.get("BROWSER_USE_API_KEY"))
        try:
            if self.config.cloud_api == "v3":
                result = await _run_v3(client, turn.text, self.trial.model, agent_started=self._agent_started)
            else:
                result = await _run_v4(
                    client, turn.text, self.trial.model, self.config.poll_seconds, agent_started=self._agent_started
                )
            browser_id = result.get("browser_id")
            if isinstance(browser_id, str):
                try:
                    result["final_page"] = await self._observe_cloud_browser(client, browser_id, result.get("url"))
                except Exception as error:
                    result["observe_error"] = f"{type(error).__name__}: {error}"
                stopped = await asyncio.shield(_stop(client, browser_id))
                settled = await asyncio.shield(_settled(client, browser_id)) if stopped is not None else None
                existing = result.get("cost_lines")
                lines = list(existing) if isinstance(existing, list) else []
                known = {item.get("component") for item in lines if isinstance(item, Mapping)}
                lines.extend(
                    line
                    for line in cost_lines(settled or stopped, settled=settled is not None)
                    if line["component"] not in known
                )
                result["cost_lines"] = lines
            return result
        finally:
            with contextlib.suppress(Exception):
                await client.close()

    async def _observe_cloud_browser(self, client: Any, browser_id: str, url: str | None) -> dict[str, Any]:
        view = await client.browsers.get(browser_id)
        cdp = getattr(view, "cdp_url", None)
        if not isinstance(cdp, str):
            return {"url": url or "", "title": "", "text": "", "controls": []}
        async with httpx.AsyncClient() as http:
            return await self.observe(await devtools_websocket(http, cdp), url=url)


async def _run_v4(
    client: Any,
    goal: str,
    model: str | None,
    poll: float,
    *,
    agent_started: Callable[[], None] | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {"model": model} if model else {}
    created: Any | None = None
    run_id: str | None = None
    browser_id: str | None = None
    normal = False
    events: list[dict[str, Any]] = []
    try:
        try:
            created = await client.runs.create(goal, **request)
        except Exception as error:
            raise _failure("run create", error, starting=True) from None
        run_id = str(created.id)
        after: int | None = None
        visible_by = time.monotonic() + 5.0
        seen = False
        while True:
            try:
                status = await client.runs.status(run_id)
            except Exception as error:
                if getattr(error, "status_code", None) == 404 and not seen and time.monotonic() < visible_by:
                    await asyncio.sleep(min(poll, 0.25))
                    continue
                raise _failure("run status", error) from None
            seen = True
            state = _status_value(getattr(status, "status", status))
            while True:
                try:
                    page = await client.runs.events(run_id, after=after)
                except Exception as error:
                    raise _failure("run events", error) from None
                for event in getattr(page, "events", ()):
                    item = _object_dict(event)
                    events.append(item)
                    if _status_value(item.get("type")) in {"browser.ready", "browser.reattached"}:
                        data = item.get("data")
                        if isinstance(data, Mapping):
                            candidate = data.get("browser_session_id") or data.get("browser_id")
                            if isinstance(candidate, str):
                                browser_id = candidate
                                if agent_started is not None:
                                    agent_started()
                next_after = getattr(page, "next_after", None)
                if isinstance(next_after, int):
                    after = next_after
                if not getattr(page, "has_more", False):
                    break
            if state in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(poll)
        try:
            finished = await client.runs.get(run_id)
        except Exception as error:
            raise _failure("run result", error) from None
        result = _run_result(finished, events)
        session = getattr(finished, "session_id", None)
        try:
            cost = await client.sessions.cost(str(session))
        except Exception:
            logger.warning("the cost breakdown of session %s could not be read; its components are unknown", session)
            result["cost_lines"] = [{"component": component, "dollars": None} for component in _COST_FIELDS.values()]
        else:
            result["cost_lines"] = _cost_from_view(_object_dict(cost))
        browser_id = result.get("browser_id") if isinstance(result.get("browser_id"), str) else browser_id
        if browser_id is not None:
            result["browser_id"] = browser_id
        result["ready"] = browser_id is not None
        normal = True
        return result
    except BaseException:
        if run_id is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(client.runs.cancel(run_id))
        raise
    finally:
        if not normal and browser_id is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(_stop(client, browser_id))


async def _run_v3(
    client: Any,
    goal: str,
    model: str | None,
    *,
    agent_started: Callable[[], None] | None = None,
) -> dict[str, Any]:
    session_id: str | None = None
    try:
        try:
            created = await client.sessions.create(task=goal, **({"model": model} if model else {}))
        except Exception as error:
            raise _failure("session create", error, starting=True) from None
        session_id = str(created.id)
        if agent_started is not None:
            agent_started()
        while True:
            try:
                view = await client.sessions.get(session_id)
            except Exception as error:
                raise _failure("session status", error) from None
            state = _status_value(getattr(view, "status", view))
            if state in {"completed", "idle", "failed", "stopped", "timed_out", "cancelled"}:
                result = _run_result(view, [])
                try:
                    browsers = await client.browsers.list(agent_session_id=session_id)
                except Exception as error:
                    raise _failure("session browser", error) from None
                for browser in getattr(browsers, "items", ()):
                    browser_id = getattr(browser, "id", None)
                    if browser_id is not None:
                        result["browser_id"] = str(browser_id)
                        break
                return result
            await asyncio.sleep(1)
    except BaseException:
        if session_id is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(client.sessions.stop(session_id))
        raise


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value)).lower()


def _object_dict(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return {key: getattr(value, key) for key in ("id", "type", "url", "browser_id", "output") if hasattr(value, key)}


def _run_result(view: object, events: list[dict[str, Any]]) -> dict[str, Any]:
    raw = _object_dict(view)
    status = _status_value(raw.get("status", "error"))
    browser_id = raw.get("browserId") or raw.get("browser_id") or raw.get("browser_session_id")
    dispatch_failed = any(event.get("type") == "run.dispatch_failed" for event in events)
    answer = raw.get("output") or raw.get("result") or raw.get("finalResult")
    if isinstance(answer, Mapping):
        answer = answer.get("text") or answer.get("answer")
    steps = [event for event in events if str(event.get("type", "")).endswith("step")]
    return {
        "status": "done"
        if status in {"completed", "idle"}
        else "stopped"
        if status in {"cancelled", "stopped", "timed_out"}
        else "unavailable"
        if dispatch_failed
        else "error",
        "answer": answer if isinstance(answer, str) else None,
        "browser_id": browser_id,
        "url": raw.get("url"),
        "step_log": steps,
        "error": raw.get("error"),
        "actual_model": raw.get("model"),
        "cost_lines": _cost_from_view(raw),
    }


_COST_FIELDS = {
    "llm_cost_usd": "model",
    "search_cost_usd": "search",
    "browser_cost_usd": "browser",
    "proxy_cost_usd": "proxy",
}
"""Per-component cost fields of a V3 session and a V4 session cost read, by Parallax component."""


def _cost_from_view(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"component": component, "dollars": view[key]} for key, component in _COST_FIELDS.items() if key in view]


class BrowserHarnessOSS(BrowserHarness):
    info = _info(
        "browser-use-oss",
        pins_model=True,
        credential_env=("BROWSER_USE_API_KEY",),
        install="pip install --no-cache-dir 'browser-use==1.0.3'",
    )

    async def execute(self, turn: TurnInput) -> dict[str, Any]:
        request = {"goal": turn.text, "model": self.trial.model, "max_steps": None, "timeout": None}
        worker = JsonWorker(
            (sys.executable, "-m", "parallax.harnesses.browser.oss_worker"),
            env=dict(os.environ) | dict(self.trial.env),
            cwd=self.trial.home,
        )
        async with worker:
            await worker.send(request)
            ready = await worker.receive("ready")
            self._agent_started(ready.get("open_seconds"))
            result = await worker.receive("result")
            result["ready"] = True
            result["open_seconds"] = ready.get("open_seconds")
            if isinstance(ready.get("cdp_url"), str):
                result["final_page"] = await self.observe(ready["cdp_url"], url=result.get("url"))
            await worker.send({})
            stopped = await worker.receive("stopped")
            result["stopped"] = stopped.get("stopped", False)
            return result


class BrowserHarnessRunner(BrowserHarness):
    """Base for workers that use a cloud browser and an external command."""

    command_env = "PARALLAX_BROWSER_HARNESS_COMMAND"
    info = _info("browser-harness", pins_model=True, credential_env=("BROWSER_USE_API_KEY",))

    async def execute(self, turn: TurnInput) -> dict[str, Any]:
        async with httpx.AsyncClient() as http, cloud_browser(http, screen=(1120, 780)) as browser:
            self._browser = browser
            request = {"goal": turn.text, "model": self.trial.model, "cdp_url": browser.cdp_url}
            configured = os.environ.get(self.command_env)
            command = (
                tuple(shlex.split(configured))
                if configured
                else (sys.executable, "-m", "parallax.harnesses.browser.browser_harness_worker")
            )
            worker_env = (
                dict(os.environ)
                | dict(self.trial.env)
                | {
                    "BU_CDP_WS": browser.cdp_url,
                    "PARALLAX_BROWSER_CDP_URL": browser.cdp_url,
                }
            )
            worker = JsonWorker(command, env=worker_env, cwd=self.trial.home)
            async with worker:
                await worker.send(request)
                self._agent_started(browser.open_seconds)
                result = await worker.receive("result")
                result["ready"] = True
                result["open_seconds"] = browser.open_seconds
                result["final_page"] = await self.observe(browser.cdp_url, url=result.get("url"))
                await worker.send({})
                with contextlib.suppress(RuntimeError):
                    await worker.receive("stopped")
        # The browser settles its charges as its context exits, so they are read only after it closes.
        result["cost_lines"] = list(browser.cost)
        return result


class CuaCodexHarness(BrowserHarness):
    info = _info("cua-codex", pins_model=True)

    async def execute(self, turn: TurnInput) -> dict[str, Any]:
        request = {
            "goal": turn.text,
            "model": self.trial.model,
            "start_url": os.environ.get("PARALLAX_BROWSER_START_URL"),
        }
        worker = JsonWorker(
            (sys.executable, "-m", "parallax.harnesses.browser.cua_worker"),
            env=dict(os.environ) | dict(self.trial.env),
            cwd=self.trial.home,
        )
        async with worker:
            await worker.send(request)
            ready = await worker.receive("ready")
            self._agent_started(ready.get("open_seconds"))
            result = await worker.receive("result")
            result["ready"] = True
            result["open_seconds"] = ready.get("open_seconds")
            if isinstance(ready.get("cdp_url"), str):
                result["final_page"] = await self.observe(ready["cdp_url"], url=result.get("url"))
            await worker.send({})
            with contextlib.suppress(RuntimeError):
                await worker.receive("stopped")
            return result
