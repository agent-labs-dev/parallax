"""Browser harness implementations.

The classes deliberately keep provider SDK imports inside the execution methods.
Importing Parallax, listing harnesses, and running offline tests therefore never
requires browser credentials or a third-party browser package.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import shlex
import shutil
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict

from parallax.harnesses.base import HarnessError, config_hash
from parallax.harnesses.browser import oss_worker
from parallax.harnesses.browser.browser_harness_worker import Agent
from parallax.harnesses.browser.clock import SESSION_INCLUDED, AttemptClock
from parallax.harnesses.browser.cloud import (
    _failure,
    _stop,
    browser_minutes,
    cloud_browser,
    devtools_websocket,
    opened,
    stopped_browser_cost,
    with_stopped_browser_cost,
)
from parallax.harnesses.browser.observer import AmbiguousFinalPage, load_observer
from parallax.harnesses.browser.site import Unavailable, browser_site_down
from parallax.harnesses.browser.subprocess import JsonWorker
from parallax.harnesses.browser.toolchain import Cli, Toolchain, provision
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
    # Sent on every V4 run, so no vendor default decides an arm's limits: Browser Use caps a session at $25 and
    # runs its fast presets without a proxy unless told otherwise. The cap sits far above any run's cost.
    max_cost_usd: float = 1000.0
    proxy_country: str = "us"


def _info(
    name: str,
    *,
    source_url: str,
    pins_model: bool,
    credentials: bool = False,
    credential_env: tuple[str, ...] = (),
    install: str = "",
    isolation: Isolation = Isolation.DOCKER,
) -> HarnessInfo:
    capabilities = {Capability.BROWSER}
    if credentials:
        capabilities.add(Capability.CREDENTIALS)
    return HarnessInfo(
        name=name,
        kind=HarnessKind.OPEN,
        capabilities=frozenset(capabilities),
        pins_model=pins_model,
        cost_source=CostSource.HARNESS,
        isolation=isolation,
        install=install,
        credential_env=credential_env,
        source_url=source_url,
    )


def _step_name(raw: Mapping[str, Any]) -> str | None:
    """What a step did. Workers name it under different keys: `name` (agent tool calls), `action` (product arms and
    the evidence timeline), `operation` or `tool` (Browser Use)."""
    return next((raw[key] for key in ("name", "action", "operation", "tool") if isinstance(raw.get(key), str)), None)


def _step_value(raw: Mapping[str, Any]) -> str | None:
    """What the step was given, as the judge reads it: a `value`, else the code or command it ran, else its
    arguments whole."""
    if isinstance(raw.get("value"), str):
        return raw["value"]
    args = raw.get("args", raw.get("arguments"))
    if isinstance(args, Mapping):
        ran = next((args[key] for key in ("code", "command") if isinstance(args.get(key), str)), None)
        return ran if ran is not None else json.dumps(args, ensure_ascii=False, default=str)
    if isinstance(args, list):
        return " ".join(str(part) for part in args)
    return args if isinstance(args, str) else None


def _step_quote(raw: Mapping[str, Any]) -> str | None:
    """What the step returned: a `quote`, else its output or observation; a tool result given as content blocks keeps
    its text."""
    for key in ("quote", "output", "observation"):
        value = raw.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            texts = [
                block["text"] for block in value if isinstance(block, Mapping) and isinstance(block.get("text"), str)
            ]
            if texts:
                return "\n".join(texts)
    return None


def _step(turn: int, raw: Mapping[str, Any]) -> Step:
    kind = raw.get("kind", "tool_call")
    try:
        parsed_kind = StepKind(kind)
    except ValueError:
        parsed_kind = StepKind.TOOL_CALL
    return Step(
        turn=turn,
        kind=parsed_kind,
        name=_step_name(raw),
        args=raw.get("args", raw.get("target", raw.get("arguments"))),
        output=raw.get("output", raw.get("observation")),
        ok=raw.get("ok"),
        t_ms=int(raw.get("t_ms", 0)),
    )


def _steps(turn: int, raw: object) -> tuple[Step, ...]:
    return tuple(_step(turn, item) for item in raw if isinstance(item, Mapping)) if isinstance(raw, list) else ()


def _mapping_list(raw: object) -> list[dict[str, Any]]:
    return [dict(item) for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _answer_text(value: object, *, structured: bool) -> str | None:
    """A run's answer as text. A structured answer (the task declared a schema) is serialised whole; an unstructured
    one may arrive wrapped as `{"text": ...}` or `{"answer": ...}`, and only then is the wrapper unwrapped."""
    if isinstance(value, str):
        return value
    if value is None:
        return None
    if isinstance(value, Mapping) and not structured:
        nested = value.get("text") or value.get("answer")
        if nested is not None:
            return _answer_text(nested, structured=False)
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return None


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
                    component=str(item["component"]) if item.get("component") else None,
                    input=int(item.get("input", item.get("input_tokens", 0))),
                    output=int(item.get("output", item.get("output_tokens", 0))),
                    cache_read=int(item.get("cache_read", item.get("cached_input_tokens", 0))),
                    cache_write=int(item.get("cache_write", 0)),
                    reasoning=int(item.get("reasoning", 0)),
                    requests=int(item.get("requests", 1)),
                    images=int(item.get("images", 0)),
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


def _page_record(page: Mapping[str, Any]) -> dict[str, Any]:
    record = {key: page[key] for key in ("url", "title", "text", "screenshot_png_b64", "controls") if key in page}
    record.setdefault("url", "")
    record.setdefault("title", "")
    record.setdefault("text", "")
    return record


def _evidence_payload(
    *,
    page: Mapping[str, Any] | None,
    answer: str | None,
    steps: object,
    citations: object = (),
    pages: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    final = None if page is None else _page_record(page)
    final_url = "" if final is None else str(final.get("url", ""))
    timeline = []
    if isinstance(steps, list):
        for item in steps:
            if not isinstance(item, Mapping):
                continue
            action = _step_name(item)
            if action is None:
                continue
            step = {"url": str(item.get("url") or final_url), "action": action}
            for key, read in (("value", _step_value), ("quote", _step_quote)):
                if (text := read(item)) is not None:
                    step[key] = text
            if isinstance(item.get("page"), int):
                step["page"] = item["page"]
            timeline.append(step)
    payload = {
        "final_page": final,
        "answer": answer,
        "steps": timeline,
        "pages": [_page_record(read) for read in pages],
        # Projected like the steps: a harness's own citation fields (ids, deep links) are not evidence.
        "citations": [
            {"url": item["url"], "quote": item["quote"]}
            for item in citations
            if isinstance(item, Mapping) and isinstance(item.get("url"), str) and isinstance(item.get("quote"), str)
        ]
        if isinstance(citations, list)
        else [],
    }
    try:
        from parallax.browser_evidence import BrowserEvidence
    except ImportError:
        return payload
    evidence = BrowserEvidence.model_validate(payload)
    normalized = evidence.model_dump(mode="json", exclude_none=True)
    # `answer` is nullable but required: navigation-only runs still have browser evidence even when no prose was returned.
    normalized["answer"] = evidence.answer
    return normalized


class BrowserHarness(ABC):
    """Common Parallax protocol and evidence hooks for a browser runner."""

    info: HarnessInfo

    def __init__(self, *, observer: str | None = None) -> None:
        self.config = BrowserConfig(
            harness=self.info.name,
            observer=observer or os.environ.get("PARALLAX_BROWSER_OBSERVER", "plain-cdp"),
            cloud_api=os.environ.get("PARALLAX_BROWSER_CLOUD_API", "v4"),
            poll_seconds=float(os.environ.get("PARALLAX_BROWSER_POLL_SECONDS", "1")),
            max_cost_usd=float(os.environ.get("PARALLAX_BROWSER_MAX_COST_USD", "1000")),
            proxy_country=os.environ.get("PARALLAX_BROWSER_PROXY_COUNTRY", "us"),
        )
        self._trial: Trial | None = None
        self._clock: AttemptClock | None = None
        self._answer: str | None = None
        self._page: dict[str, Any] | None = None
        self._steps: list[dict[str, Any]] = []
        self._citations: list[dict[str, Any]] = []
        self._pages: list[dict[str, Any]] = []
        self._open_tabs: list[dict[str, Any]] = []
        self._cost: dict[str, float | None] = {}
        self._details: dict[str, Any] = {"observer": self.config.observer}
        self._start_url: str | None = None
        self._start_host: str | None = None
        self._task_secrets: dict[str, str] = {}
        self._secret_values: dict[str, str] = {}
        self._secret_bindings: list[dict[str, Any]] = []
        self._answer_schema: dict[str, Any] | None = None
        self._agent_has_started = False
        self._finished_at: float | None = None  # this turn's agent returned its answer
        self._turn_agent_at: float | None = None  # this turn's agent started: its first ready, else the turn's start
        self._turn_ready = False
        self._turn_ends_at: float | None = None  # when the runner cancels this turn; None: no limit
        # Per turn: something billable was created, and whether that turn reported its charges.
        self._turn_spending = False
        self._turn_priced = False
        self._unpriced_turns = False
        self._worker_dir: Path | None = None
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

    @property
    def worker_dir(self) -> Path:
        """The working directory of a worker this harness runs on the host, one per trial.

        Not `trial.home`: in a Docker box that is the container's path, which does not exist on the host."""
        if self._worker_dir is None:
            self._worker_dir = Path(tempfile.mkdtemp(prefix=f"parallax-{self.info.name}-"))
        return self._worker_dir

    def bind_task(self, task: Task) -> None:
        """Remember a source task's secrets, answer schema and start page, where a secret is bound and an outage judged."""
        self._task_secrets = dict(task.secrets)
        self._answer_schema = task.answer_schema
        if task.meta.area is Area.BROWSER:
            value = task.data.get("website", task.data.get("start_url"))
            self._start_url = value if isinstance(value, str) else None
            self._start_host = urlsplit(self._start_url).hostname if self._start_url is not None else None

    async def start(self, trial: Trial, box: Any) -> None:
        self._trial = trial
        self._clock = AttemptClock(bounded=False)
        self._details["task_id"] = trial.task_id
        self._details["model"] = trial.model
        self._secret_values = {}
        self._secret_bindings = []
        if self._task_secrets and self._start_host is None:
            raise RuntimeError("task secrets need a start URL with a host")
        for alias, env_name in self._task_secrets.items():
            value = os.environ.get(env_name)
            if not value:
                raise RuntimeError(f"missing secret environment variable {env_name} for {alias}")
            self._secret_values[alias] = value
            self._secret_bindings.append(
                {
                    "alias": alias,
                    "source": {"type": "inline", "value": value},
                    "allowedDomains": [self._start_host],
                }
            )

    def _scrub(self, value: object) -> Any:
        if isinstance(value, str):
            for alias, secret in sorted(self._secret_values.items(), key=lambda item: len(item[1]), reverse=True):
                value = value.replace(secret, f"<secret>{alias}</secret>")
            return value
        if isinstance(value, Mapping):
            return {key: self._scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._scrub(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._scrub(item) for item in value)
        return value

    def _agent_env(self) -> dict[str, str]:
        """Give browser workers the host environment minus every task-secret variable."""
        secret_names = set(self._task_secrets.values())
        env = dict(os.environ) | dict(self.trial.env)
        return {name: value for name, value in env.items() if name not in secret_names}

    async def turn(self, turn: TurnInput) -> TurnOutput:
        began = time.monotonic()
        self._turn_ends_at = None if turn.deadline_s is None else began + turn.deadline_s
        try:
            # A site's outage is judged by the browser the trial browses with, never a plain HTTP request from the
            # host, which a site behind bot protection refuses although the browser loads it. Where Parallax opens that
            # browser, `browser_site_down` asks it; a product that brings its own reports `unavailable` itself.
            # The clock starts now; spending starts only when `execute` reports its agent started.
            self._clock_started()
            self._turn_spending = self._turn_priced = False
            self._finished_at = None
            self._turn_agent_at, self._turn_ready = time.monotonic(), False
            result = await self.execute(turn)
        except asyncio.CancelledError:
            # The runner's turn limit cancelled the agent. Keep the pages and steps it reached, so the judge can see
            # where it stopped and attribute the timeout, instead of grading a run that left no record.
            try:
                self._keep_evidence(self.cancelled())
            except Exception as error:  # the cancellation, not this, is what the runner must see
                self.trial_notes.append(f"cancelled turn's evidence not kept: {type(error).__name__}: {error}")
            raise
        except Unavailable as error:
            result = {"status": "unavailable", "answer": None, "error": str(error)}
        except Exception as error:
            result = {"status": "error", "answer": None, "error": f"{type(error).__name__}: {error}"}
        status = _normalise_status(result.get("status"))
        answer = self._scrub(result.get("answer"))
        self._answer = _answer_text(answer, structured=self._answer_schema is not None)
        self._keep_evidence(result)
        if isinstance(result.get("observe_error"), str):
            self.trial_notes.append(f"final page not observed: {self._scrub(result['observe_error'])}")
        if self._clock is not None and (status is TurnStatus.COMPLETED or self._finished_at is not None):
            # Agent time ends when the agent answered; reading the final page and closing the browser are ours.
            self._clock.answered(self._finished_at)
        if isinstance(lines := result.get("cost_lines"), list):
            self._record_costs(lines)
        if self._turn_spending and not self._turn_priced:
            # An earlier turn's known charges must not stand for this turn's, which may have billed.
            self._unpriced_turns = True
        self._details.update(
            {
                "status": str(result.get("status", "error")),
                "error": self._scrub(result.get("error")),
                "steps": len(self._steps),
                "wall_seconds": round(time.monotonic() - began, 2),
            }
        )
        interruption = _interruption(result)
        error = interruption or (self._scrub(str(result["error"])) if result.get("error") is not None else None)
        latency = result.get("latency_ms")
        latency_ms = int(float(latency)) if isinstance(latency, int | float) else None
        if latency_ms is None and self._finished_at is not None and self._turn_agent_at is not None:
            # The runner's agent time is this, else the whole turn, which would include our cleanup.
            latency_ms = int((self._finished_at - self._turn_agent_at) * 1000)
        return TurnOutput(
            status=status,
            answer=self._answer or "",
            session_id=result.get("session_id"),
            steps=_steps(turn.index, self._scrub(result.get("step_log"))),
            usage=_usage(result.get("usage"), self.trial.model),
            actual_model=result.get("actual_model") if isinstance(result.get("actual_model"), str) else None,
            # A harness that runs several models (a writer and a classifier) names each one it ran.
            actual_models=tuple(m for m in result.get("actual_models") or () if isinstance(m, str)),
            latency_ms=latency_ms,
            error=error,
        )

    def _keep_evidence(self, result: Mapping[str, Any]) -> None:
        """Record a turn's pages, steps, citations and final page from `execute`'s result."""
        # A turn's steps point into that turn's `pages`; across turns the pages accumulate, so the indices shift.
        offset = len(self._pages)
        self._pages.extend(
            # As on the final page: text replacement cannot reach pixels.
            {key: value for key, value in read.items() if key != "screenshot_png_b64"} if self._secret_values else read
            for read in self._scrub(_mapping_list(result.get("pages")))
        )
        self._steps.extend(
            {**step, "page": step["page"] + offset} if isinstance(step.get("page"), int) else step
            for step in self._scrub(_mapping_list(result.get("step_log")))
        )
        self._citations.extend(self._scrub(_mapping_list(result.get("citations"))))
        if self._open_tabs:
            self._page = None  # this turn ended on one of the tabs, not on an earlier turn's final page
            self._pages.extend(
                {key: value for key, value in tab.items() if key != "screenshot_png_b64"}
                if self._secret_values
                else tab
                for tab in self._scrub(self._open_tabs)
            )
            self._open_tabs = []
        page = result.get("final_page")
        if isinstance(page, Mapping):
            self._page = self._scrub(dict(page))
            if self._secret_values and self._page.pop("screenshot_png_b64", None) is not None:
                # Text replacement cannot reach pixels, and a page can show a bound login; the judge reads its text.
                self.trial_notes.append("final-page screenshot omitted: the task binds secrets")

    def seconds_left(self) -> float | None:
        """What is left of this turn's `deadline_s`, for `execute` to give a product that can stop itself; None when
        the turn has no limit. Reading the final page and closing the browser come after, so a product that uses all
        of it can still be cancelled: `cancelled()` keeps its evidence then."""
        return None if self._turn_ends_at is None else max(0.0, self._turn_ends_at - time.monotonic())

    def cancelled(self) -> Mapping[str, Any]:
        """What the agent reached before the runner cancelled its turn, in the keys `execute` returns (`pages`,
        `step_log`, `citations`, `final_page`). Called during the cancellation, so it must not await. Empty for a
        harness that keeps no running record."""
        return {}

    async def stop(self) -> None:
        if self._clock is not None:
            self._clock.closed_at()
        try:
            await self.cleanup()
        finally:
            if self._worker_dir is not None:
                shutil.rmtree(self._worker_dir, ignore_errors=True)
                self._worker_dir = None

    async def version(self, box: Any) -> str:
        return self.info.name

    @property
    def trial_metadata(self) -> dict[str, Any]:
        return {
            "browser": _evidence_payload(
                page=self._page, answer=self._answer, steps=self._steps, citations=self._citations, pages=self._pages
            )
        }

    def trial_timing(self) -> Timing:
        return self._clock.timing() if self._clock is not None else Timing(basis=SESSION_INCLUDED, breakdown={})

    def trial_costs(self) -> dict[str, float | None]:
        if not self._agent_has_started and not self._cost:
            return {"model": 0.0}
        if self._unpriced_turns:
            return dict(self._cost) | {"unpriced turn": None}
        return dict(self._cost)

    def _record_costs(self, lines: list[Any]) -> None:
        costs = _known_costs(lines)
        self._cost.update(costs)
        # An empty report prices nothing: a worker sends `[]` when its run's cost is unknown.
        self._turn_priced = self._turn_priced or bool(costs)

    def trial_details(self) -> dict[str, Any]:
        return dict(self._details)

    @abstractmethod
    async def execute(self, turn: TurnInput) -> dict[str, Any]: ...

    async def cleanup(self) -> None:
        return None

    async def observe(self, cdp_url: str, *, url: str | None = None) -> dict[str, Any] | None:
        """The page the browser ended on. When several tabs stay open and none is named, there is no single final
        page: each tab joins the run's `pages` instead, so the judge reads every page the run could have ended on."""
        try:
            return await load_observer(self.config.observer).observe(cdp_url, url=url)
        except AmbiguousFinalPage as error:
            self._open_tabs = error.tabs
            self.trial_notes.append(f"final page not singled out: {len(error.tabs)} tabs open, each kept as a page")
            return None

    def _browser_opened(self, browser_id: object, live_url: object = None) -> None:
        """Record a Browser Use cloud browser the trial ran on, with its live view link. Browser Use clears the link
        once the browser stops, so it is read while the browser is open; a trial with several turns has one each."""
        if isinstance(browser_id, str):
            browser = {"browser_id": browser_id, "live_url": live_url if isinstance(live_url, str) else None}
            self._details.setdefault("browsers", []).append(browser)

    def _agent_started(self, open_seconds: object = None) -> None:
        """The agent is running and may spend: a worker reported ready, or a cloud task exists."""
        self._spending()
        if not self._turn_ready:
            # Only the first: a cloud run reports `browser.reattached` after `browser.ready`, mid-turn.
            self._turn_agent_at, self._turn_ready = time.monotonic(), True
        self._clock_started(open_seconds)

    def _agent_finished(self) -> None:
        """The agent returned: what `execute` does after this (observing, stopping, settling) is not agent time."""
        if self._finished_at is None:
            self._finished_at = time.monotonic()

    def _spending(self) -> None:
        """Something billable exists for this attempt, so an absent cost is unknown, not zero."""
        self._agent_has_started = True
        self._turn_spending = True

    def _clock_started(self, open_seconds: object = None) -> None:
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
    # A pinned model: omitted, the server picks the project's default, which depends on when the project was made.
    info = _info(
        "browser-use-cloud",
        source_url="https://docs.browser-use.com",
        pins_model=True,
        credentials=True,
        credential_env=("BROWSER_USE_API_KEY",),
    )

    def __init__(self, *, observer: str | None = None) -> None:
        super().__init__(observer=observer)
        if self.config.cloud_api != "v4":
            # Only the V4 API takes secret bindings, so V3 cannot attempt a task that needs a protected login.
            self.info = self.info.model_copy(update={"capabilities": self.info.capabilities - {Capability.CREDENTIALS}})

    async def execute(self, turn: TurnInput) -> dict[str, Any]:
        client_module = importlib.import_module(f"browser_use_sdk.{self.config.cloud_api}")
        client_name = "AsyncBrowserUse"
        client_cls = getattr(client_module, client_name)
        client = client_cls(api_key=self.trial.env.get("BROWSER_USE_API_KEY") or os.environ.get("BROWSER_USE_API_KEY"))
        try:
            if self.config.cloud_api == "v3":
                result = await _run_v3(
                    client,
                    turn.text,
                    self.trial.model,
                    agent_started=self._agent_started,
                    finished=self._agent_finished,
                )
            else:
                result = await _run_v4(
                    client,
                    turn.text,
                    self.trial.model,
                    self.config.poll_seconds,
                    agent_started=self._agent_started,
                    spending=self._spending,
                    finished=self._agent_finished,
                    output_schema=self._answer_schema,
                    secret_bindings=self._secret_bindings or None,
                    max_cost_usd=self.config.max_cost_usd,
                    proxy_country=self.config.proxy_country,
                )
                self._details.update(
                    {"max_cost_usd": self.config.max_cost_usd, "proxy_country": self.config.proxy_country}
                )
            actual = result.get("actual_model")
            self._details["actual_model"] = actual
            if not isinstance(actual, str):
                self.trial_notes.append("the run did not report the model it ran")
            elif actual != self.trial.model:
                # The row would be filed under a model that never ran.
                self.trial_invalid.append(f"asked for model {self.trial.model}, the run reports {actual}")
            browser_id = result.get("browser_id")
            if isinstance(browser_id, str):
                try:
                    try:
                        result["final_page"] = await self._observe_cloud_browser(client, browser_id, result)
                    except Exception as error:
                        result["observe_error"] = f"{type(error).__name__}: {error}"
                finally:
                    # Also when the turn is cancelled while the page is read: the run is over, its browser is not.
                    stopped = await stopped_browser_cost(browser_id, client=client)
                existing = result.get("cost_lines")
                result["cost_lines"] = with_stopped_browser_cost(
                    existing if isinstance(existing, list) else [], stopped
                )
            return result
        finally:
            with contextlib.suppress(Exception):
                await client.close()

    async def _observe_cloud_browser(
        self, client: Any, browser_id: str, result: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """The page the run's browser ended on. The run is over but its browser is still open, so its live view link
        is read here too; a V3 run gives the link when its session is created, and that one is kept."""
        url = result.get("url")
        try:
            view = await client.browsers.get(browser_id)
        except BaseException:
            self._browser_opened(browser_id, result.get("live_url"))
            raise
        self._browser_opened(browser_id, result.get("live_url") or getattr(view, "live_url", None))
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
    spending: Callable[[], None] | None = None,
    finished: Callable[[], None] | None = None,
    output_schema: dict[str, Any] | None = None,
    secret_bindings: list[dict[str, Any]] | None = None,
    max_cost_usd: float | None = None,
    proxy_country: str | None = None,
) -> dict[str, Any]:
    """`spending` is called once the cloud run exists, which may bill before its browser is ready; `finished` once
    the run reaches a terminal status, before its result and costs are read."""
    request: dict[str, Any] = {"model": model} if model else {}
    if max_cost_usd is not None:
        request["max_cost_usd"] = max_cost_usd
    if proxy_country is not None:
        request["browser_settings"] = {"proxyCountryCode": proxy_country}
    if output_schema is not None:
        request["output_schema"] = output_schema
    if secret_bindings is not None:
        request["secret_bindings"] = secret_bindings
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
        if spending is not None:
            spending()
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
                                opened(browser_id, client)
                                if agent_started is not None:
                                    agent_started()
                next_after = getattr(page, "next_after", None)
                if isinstance(next_after, int):
                    after = next_after
                if not getattr(page, "has_more", False):
                    break
            if state in {"completed", "failed", "cancelled"}:
                if finished is not None:
                    finished()
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
    finished: Callable[[], None] | None = None,
) -> dict[str, Any]:
    session_id: str | None = None
    try:
        try:
            created = await client.sessions.create(task=goal, **({"model": model} if model else {}))
        except Exception as error:
            raise _failure("session create", error, starting=True) from None
        session_id = str(created.id)
        live_url = getattr(created, "live_url", None)  # only while the session runs: Browser Use clears it after
        if agent_started is not None:
            agent_started()
        while True:
            try:
                view = await client.sessions.get(session_id)
            except Exception as error:
                raise _failure("session status", error) from None
            state = _status_value(getattr(view, "status", view))
            if state in {"completed", "idle", "failed", "stopped", "timed_out", "cancelled"}:
                if finished is not None:
                    finished()
                result = _run_result(view, [])
                result["live_url"] = live_url
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
    answer = raw.get("output")
    if answer is None:
        answer = raw.get("result")
    if answer is None:
        answer = raw.get("finalResult")
    steps = _v4_steps(events)
    return {
        "status": "done"
        if status in {"completed", "idle"}
        else "stopped"
        if status in {"cancelled", "stopped", "timed_out"}
        else "unavailable"
        if dispatch_failed
        else "error",
        "answer": answer,
        "browser_id": browser_id,
        "url": raw.get("url"),
        "step_log": steps,
        "error": raw.get("error"),
        "actual_model": raw.get("model"),
        "cost_lines": _cost_from_view(raw),
    }


def _v4_steps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The run's tool calls, from its V4 events. A V4 run reports each finished call as a `core.event` whose `part` is
    a `tool` part with a final state; the `running` part that precedes it repeats its input, so only finished
    parts count."""
    steps: list[dict[str, Any]] = []
    for event in events:
        data = event.get("data")
        part = data.get("part") if event.get("type") == "core.event" and isinstance(data, Mapping) else None
        if not isinstance(part, Mapping) or part.get("type") != "tool":
            continue
        state = part.get("state")
        status = state.get("status") if isinstance(state, Mapping) else None
        if status not in {"completed", "error"}:
            continue
        tool = str(part.get("tool") or "tool")
        call = state.get("input")
        code = call.get("code") if isinstance(call, Mapping) else None
        output = state.get("output") if status == "completed" else state.get("error")
        step: dict[str, Any] = {"tool": tool, "action": tool, "args": call, "ok": status == "completed"}
        if isinstance(code, str):
            step["value"] = code
        if isinstance(output, str):
            step["output"] = output
            step["quote"] = output
        steps.append(step)
    return steps


_COST_FIELDS = {
    "llm_cost_usd": "model",
    "search_cost_usd": "search",
    "browser_cost_usd": "browser",
    "proxy_cost_usd": "proxy",
}
"""Per-component cost fields of a V3 session and a V4 session cost read, by Parallax component."""


def _cost_from_view(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"component": component, "dollars": view[key]} for key, component in _COST_FIELDS.items() if key in view]


def oss_python() -> tuple[str, ...]:
    """The Python that runs the browser-use worker: this one when it has the pinned browser-use, else a uv
    environment with exactly that version. The worker script imports only the standard library and browser_use."""
    import importlib.metadata

    from parallax.harnesses.browser import pins

    pinned = pins.load()["browser-use"]["version"]
    try:
        if importlib.metadata.version("browser-use") == pinned:
            return (sys.executable,)
    except importlib.metadata.PackageNotFoundError:
        pass
    uv = shutil.which("uv")
    if uv is None:
        raise HarnessError(f"browser-use-oss needs browser-use=={pinned} in {sys.executable} or uv to install it")
    return (uv, "run", "--no-project", "--quiet", "--with", f"browser-use=={pinned}", "python")


class BrowserHarnessOSS(BrowserHarness):
    info = _info(
        "browser-use-oss",
        source_url="https://github.com/browser-use/browser-use",
        pins_model=True,
        credential_env=("BROWSER_USE_API_KEY",),
        install="pip install --no-cache-dir 'browser-use==1.0.6'",
    )

    async def version(self, box: Any) -> str:
        """The browser-use the worker runs, checked before any trial reserves budget. The worker runs on the host,
        not in the box the declared install builds, and browser-use pins dependencies Parallax cannot share
        (websockets), so unless this Python has the pinned package the worker runs in its own uv environment."""
        command = oss_python()
        proc = await asyncio.create_subprocess_exec(
            # The worker's own isolation (-I), and an import, not only metadata: what the worker will load.
            *command, "-I", "-c", "import browser_use, importlib.metadata as m; print(m.version('browser-use'))",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise HarnessError(f"browser-use runtime failed to start: {err.decode(errors='replace').strip()[-500:]}")
        return f"browser-use {out.decode().strip()}"

    async def execute(self, turn: TurnInput) -> dict[str, Any]:
        request = {
            "goal": turn.text,
            "model": self.trial.model,
            "max_steps": None,
            "timeout": None,
            "browser_minutes": browser_minutes(self.seconds_left()),
        }
        worker = JsonWorker(
            # -I: run by path, the script's own directory would lead sys.path, and its sibling subprocess.py would
            # shadow the standard library's.
            (*oss_python(), "-I", str(Path(oss_worker.__file__))),
            env=self._agent_env(),
            cwd=self.worker_dir,
        )
        async with worker:
            await worker.send(request)
            ready = await worker.receive("ready")
            if isinstance(ready.get("browser_id"), str):
                opened(ready["browser_id"])
            self._browser_opened(ready.get("browser_id"), ready.get("live_url"))
            self._agent_started(ready.get("open_seconds"))
            result = await worker.receive("result")
            self._agent_finished()
            result["ready"] = True
            result["open_seconds"] = ready.get("open_seconds")
            if isinstance(ready.get("cdp_url"), str):
                result["final_page"] = await self.observe(ready["cdp_url"], url=result.get("url"))
            await worker.send({})
            stopped = await worker.receive("stopped")
            result["stopped"] = stopped.get("stopped", False)
        # The agent ran on a Browser Use cloud browser with a US proxy, as every other arm does; it reports only its
        # model's cost, so the browser and proxy are read from the API once that browser has stopped.
        if isinstance(ready.get("browser_id"), str):
            browser_cost = await stopped_browser_cost(ready["browser_id"])
            result["cost_lines"] = [*_mapping_list(result.get("cost_lines")), *browser_cost]
        return result


class BrowserHarnessRunner(BrowserHarness):
    """Browser Use's browser-harness skill run by Codex on a cloud browser. The model is an OpenRouter id."""

    command_env = "PARALLAX_BROWSER_HARNESS_COMMAND"
    agent = Agent.CODEX
    cli = Cli.BROWSER_HARNESS
    info = _info(
        "browser-harness",
        source_url="https://github.com/browser-use/browser-harness",
        pins_model=True,
        credential_env=("BROWSER_USE_API_KEY",),
    )

    _toolchain: Toolchain | None = None

    async def version(self, box: Any) -> str:
        """Installs the pinned browser-harness and agent, before any trial reserves budget, and reports both."""
        self._toolchain = await provision(self.agent, self.cli)
        return self._toolchain.version

    async def execute(self, turn: TurnInput) -> dict[str, Any]:
        # Before the browser opens, so a missing tool costs nothing.
        toolchain = self._toolchain or await provision(self.agent, self.cli)
        browser = None
        try:
            minutes = browser_minutes(self.seconds_left())
            async with httpx.AsyncClient() as http, cloud_browser(http, screen=(1120, 780), minutes=minutes) as browser:
                # The paid browser exists, so the attempt spends from here even if the worker never starts.
                self._browser_opened(browser.browser_id, browser.live_url)
                if self._start_url is not None and (
                    outage := await browser_site_down(browser.cdp_url, self._start_url)
                ):
                    raise Unavailable(outage)
                self._agent_started(browser.open_seconds)
                request = {
                    "goal": turn.text,
                    "model": self.trial.model,
                    "agent": self.agent,
                    "cdp_url": browser.cdp_url,
                    **toolchain.request(),
                }
                configured = os.environ.get(self.command_env)
                command = (
                    tuple(shlex.split(configured))
                    if configured
                    else (sys.executable, "-m", "parallax.harnesses.browser.browser_harness_worker")
                )
                worker_env = (
                    self._agent_env()
                    | self.cli.environment(browser.cdp_url, f"parallax-{uuid4().hex[:12]}")
                    | {"PARALLAX_BROWSER_CDP_URL": browser.cdp_url}
                )
                worker = JsonWorker(command, env=worker_env, cwd=self.worker_dir)
                async with worker:
                    await worker.send(request)
                    result = await worker.receive("result")
                    self._agent_finished()
                    result["ready"] = True
                    result["open_seconds"] = browser.open_seconds
                    result["final_page"] = await self.observe(browser.cdp_url, url=result.get("url"))
                    await worker.send({})
                    with contextlib.suppress(RuntimeError):
                        await worker.receive("stopped")
        except BaseException:
            # The worker failed, but the browser it was given has already settled its charges.
            if browser is not None:
                self._record_costs(list(browser.cost))
            raise
        # The browser settles its charges as its context exits, so they are read only after it closes. The worker's own
        # lines price the agent's model, which the trial's cost would otherwise leave out.
        result["cost_lines"] = [*_mapping_list(result.get("cost_lines")), *browser.cost]
        return result


class AgentBrowserRunner(BrowserHarnessRunner):
    """Vercel's agent-browser CLI and its skill run by Codex on a cloud browser, as browser-harness is: the same
    agent, model routing, browser and cost accounting, a different CLI."""

    cli = Cli.AGENT_BROWSER
    info = _info(
        "agent-browser",
        source_url="https://github.com/vercel-labs/agent-browser",
        pins_model=True,
        credential_env=("BROWSER_USE_API_KEY",),
    )


class BrowserHarnessClaudeCode(BrowserHarnessRunner):
    """browser-harness run by Claude Code, for comparing the two agents on the same skill and browser."""

    agent = Agent.CLAUDE_CODE
    info = _info(
        "browser-harness-claude-code",
        source_url="https://github.com/browser-use/browser-harness",
        pins_model=True,
        credential_env=("BROWSER_USE_API_KEY",),
    )


class CuaCodexHarness(BrowserHarness):
    info = _info("cua-codex", source_url="https://github.com/agent-labs-dev/parallax", pins_model=True)

    async def execute(self, turn: TurnInput) -> dict[str, Any]:
        request = {
            "goal": turn.text,
            "model": self.trial.model,
            "start_url": os.environ.get("PARALLAX_BROWSER_START_URL"),
        }
        worker = JsonWorker(
            (sys.executable, "-m", "parallax.harnesses.browser.cua_worker"),
            env=self._agent_env(),
            cwd=self.worker_dir,
        )
        async with worker:
            await worker.send(request)
            ready = await worker.receive("ready")
            self._agent_started(ready.get("open_seconds"))
            result = await worker.receive("result")
            self._agent_finished()
            result["ready"] = True
            result["open_seconds"] = ready.get("open_seconds")
            if isinstance(ready.get("cdp_url"), str):
                result["final_page"] = await self.observe(ready["cdp_url"], url=result.get("url"))
            await worker.send({})
            with contextlib.suppress(RuntimeError):
                await worker.receive("stopped")
            return result
