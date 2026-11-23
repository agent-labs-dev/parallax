from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from browser_use_sdk.v3 import (
    BrowserSessionItemView,
    BrowserSessionListResponse,
    BrowserSessionStatus,
    BuAgentSessionStatus,
    BuModel,
    SessionResponse,
)
from browser_use_sdk.v4 import (
    RunCreateResponse,
    RunEvent,
    RunEventsResponse,
    RunStatus,
    RunStatusResponse,
    RunSummary,
    SessionCostResponse,
)
from parallax.harnesses.browser.harnesses import (
    BrowserHarnessCloud,
    BrowserHarnessOSS,
    BrowserHarnessRunner,
    BrowserHarnessUltrafast,
    CuaCodexHarness,
    _evidence_payload,
    _interruption,
    _run_result,
    _run_v3,
    _run_v4,
)
from parallax.models import Capability, TurnInput

NOW = datetime(2026, 1, 1, tzinfo=UTC)
RUN_ID = UUID("00000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000002")
BROWSER_ID = UUID("00000000-0000-0000-0000-000000000003")


def v3_session(*, status: BuAgentSessionStatus = BuAgentSessionStatus.stopped) -> SessionResponse:
    return SessionResponse(
        id=SESSION_ID,
        status=status,
        model=BuModel.bu_mini,
        output="done",
        liveUrl="https://live.test/session",
        llmCostUsd="0.11",
        proxyCostUsd="0.02",
        browserCostUsd="0.03",
        totalCostUsd="0.16",
        createdAt=NOW,
        updatedAt=NOW,
    )


def v3_browser_list() -> BrowserSessionListResponse:
    return BrowserSessionListResponse(
        items=[
            BrowserSessionItemView(
                id=BROWSER_ID,
                status=BrowserSessionStatus.stopped,
                liveUrl="https://live.test/session",
                agentSessionId=SESSION_ID,
                timeoutAt=NOW,
                startedAt=NOW,
                extensions=[],
            )
        ],
        totalItems=1,
        pageNumber=1,
        pageSize=1,
    )


def v4_run_summary(*, total_cost_usd: str = "0.42") -> RunSummary:
    return RunSummary(
        id=RUN_ID,
        task="find it",
        title=None,
        model="provider/model",
        contextLimit=1000,
        status=RunStatus.completed,
        result="done",
        error=None,
        sessionId=SESSION_ID,
        workspaceId=None,
        totalInputTokens=1,
        totalOutputTokens=1,
        totalCostUsd=total_cost_usd,
        createdAt=NOW,
        updatedAt=NOW,
    )


def test_evidence_is_normalised_to_the_shared_browser_shape() -> None:
    evidence = _evidence_payload(
        page={"url": "https://site.test/", "title": "Done", "text": "Complete", "error": "ignored"},
        answer="Finished",
        steps=[{"operation": "click", "url": "https://site.test/", "observation": "Complete"}],
        citations=[{"id": 1, "url": "https://site.test/", "quote": "Complete", "deep_link": "https://site.test/#q"}],
    )
    assert evidence["final_page"] == {"url": "https://site.test/", "title": "Done", "text": "Complete"}
    assert evidence["steps"] == [{"url": "https://site.test/", "action": "click", "quote": "Complete"}]
    assert evidence["citations"] == [{"url": "https://site.test/", "quote": "Complete"}]


def test_a_coding_agents_tool_calls_reach_the_judge_with_what_they_ran_and_returned() -> None:
    evidence = _evidence_payload(
        page=None,
        answer="done",
        steps=[
            {"kind": "tool_call", "name": "shell", "args": "browser-harness click 3", "output": "clicked", "ok": True},
            {
                "kind": "tool_call",
                "name": "Bash",
                "args": {"command": "echo x"},
                "output": [{"type": "text", "text": "x"}],
            },
            {"tool": "python", "args": {"code": "await bu.state()"}, "output": "[state] Home", "ok": True},
        ],
    )
    assert [(step["action"], step["value"], step["quote"]) for step in evidence["steps"]] == [
        ("shell", "browser-harness click 3", "clicked"),
        ("Bash", "echo x", "x"),
        ("python", "await bu.state()", "[state] Home"),
    ]


def test_evidence_keeps_a_null_answer_for_navigation_only_runs() -> None:
    evidence = _evidence_payload(
        page={"url": "https://site.test/", "title": "Done", "text": "Complete"},
        answer=None,
        steps=[{"operation": "navigate", "url": "https://site.test/"}],
    )
    assert evidence["answer"] is None


def test_environment_interruptions_are_prefixed_and_agent_errors_are_not() -> None:
    assert _interruption({"status": "unavailable", "error": "provider capacity"}) == (
        "environment interruption: provider capacity"
    )
    assert _interruption({"status": "error", "error": "bad answer"}) is None


def test_observer_name_changes_harness_identity() -> None:
    assert (
        BrowserHarnessOSS(observer="plain-cdp").config_hash != BrowserHarnessOSS(observer="other-observer").config_hash
    )


async def test_v4_event_stream_retries_visibility_and_keeps_browser_identity() -> None:
    class Runs:
        def __init__(self) -> None:
            self.status_calls = 0
            self.created: dict[str, object] = {}
            self.after: list[int | None] = []

        async def create(self, goal: str, **request: object) -> RunCreateResponse:
            self.created = {"goal": goal, **request}
            return RunCreateResponse(
                id=RUN_ID,
                status=RunStatus.queued,
                model="provider/model",
                sessionId=SESSION_ID,
                workspaceId=SESSION_ID,
                eventsUrl="https://api.test/runs/events",
            )

        async def status(self, _: str) -> RunStatusResponse:
            self.status_calls += 1
            if self.status_calls == 1:
                error = RuntimeError("not visible")
                error.status_code = 404  # type: ignore[attr-defined]
                raise error
            return RunStatusResponse(status=RunStatus.completed)

        async def events(self, _: str, *, after: int | None = None) -> RunEventsResponse:
            self.after.append(after)
            if len(self.after) == 1:
                return RunEventsResponse(
                    events=[
                        RunEvent(
                            runId=RUN_ID,
                            id=1,
                            ts=NOW,
                            type="browser.ready",
                            data={"browser_session_id": "browser-1"},
                        )
                    ],
                    hasMore=True,
                    nextAfter=7,
                )
            return RunEventsResponse(
                events=[RunEvent(runId=RUN_ID, id=8, ts=NOW, type="run.step", data={"name": "click"})],
                hasMore=False,
                nextAfter=None,
            )

        async def get(self, _: str) -> RunSummary:
            return v4_run_summary()

        async def cancel(self, _: str) -> None:
            raise AssertionError("a completed run should not be cancelled")

    class Sessions:
        def __init__(self) -> None:
            self.costed: list[str] = []

        async def cost(self, session_id: str) -> SessionCostResponse:
            self.costed.append(session_id)
            return SessionCostResponse(
                sessionId=SESSION_ID,
                numRuns=1,
                llmCostUsd="0.30",
                searchCostUsd="0.01",
                browserCostUsd="0.08",
                proxyCostUsd="0.03",
                proxyUsedMb="1",
                totalCostUsd="0.42",
            )

    class Client:
        def __init__(self) -> None:
            self.runs = Runs()
            self.sessions = Sessions()

    client = Client()
    result = await _run_v4(client, "find it", "provider/model", 0)
    assert client.runs.created == {"goal": "find it", "model": "provider/model"}
    assert client.runs.after == [None, 7]
    assert result["browser_id"] == "browser-1" and result["ready"] is True
    # The run summary has only a total; the components come from the session cost read.
    assert client.sessions.costed == [str(SESSION_ID)]
    assert result["cost_lines"] == [
        {"component": "model", "dollars": "0.30"},
        {"component": "search", "dollars": "0.01"},
        {"component": "browser", "dollars": "0.08"},
        {"component": "proxy", "dollars": "0.03"},
    ]


async def test_v4_run_declares_structured_output_and_host_scoped_secret_bindings() -> None:
    class Runs:
        def __init__(self) -> None:
            self.created: dict[str, object] = {}

        async def create(self, goal: str, **request: object) -> RunCreateResponse:
            self.created = {"goal": goal, **request}
            return RunCreateResponse(
                id=RUN_ID,
                status=RunStatus.queued,
                model="provider/model",
                sessionId=SESSION_ID,
                workspaceId=SESSION_ID,
                eventsUrl="https://api.test/runs/events",
            )

        async def status(self, _: str) -> RunStatusResponse:
            return RunStatusResponse(status=RunStatus.completed)

        async def events(self, _: str, *, after: int | None = None) -> RunEventsResponse:
            return RunEventsResponse(events=[], hasMore=False, nextAfter=after)

        async def get(self, _: str) -> RunSummary:
            return v4_run_summary()

        async def cancel(self, _: str) -> None:
            raise AssertionError("a completed run should not be cancelled")

    class Sessions:
        async def cost(self, _: str) -> SessionCostResponse:
            return SessionCostResponse(
                sessionId=SESSION_ID,
                numRuns=1,
                llmCostUsd="0.30",
                searchCostUsd="0.01",
                browserCostUsd="0.08",
                proxyCostUsd="0.03",
                proxyUsedMb="1",
                totalCostUsd="0.42",
            )

    class Client:
        def __init__(self) -> None:
            self.runs = Runs()
            self.sessions = Sessions()

    schema = {"type": "object", "properties": {"price": {"type": "string"}}}
    bindings = [
        {
            "alias": "shop_password",
            "source": {"type": "inline", "value": "correct-horse-battery-staple"},
            "allowedDomains": ["shop.example"],
        }
    ]
    client = Client()
    await _run_v4(
        client,
        "sign in and find the price",
        "provider/model",
        0,
        output_schema=schema,
        secret_bindings=bindings,
        max_cost_usd=1000.0,
        proxy_country="us",
    )
    assert client.runs.created == {
        "goal": "sign in and find the price",
        "model": "provider/model",
        "output_schema": schema,
        "secret_bindings": bindings,
        # Omitted, Browser Use would cap the session at $25 and run its fast presets without a proxy.
        "max_cost_usd": 1000.0,
        "browser_settings": {"proxyCountryCode": "us"},
    }


async def test_a_structured_answer_keeps_every_field() -> None:
    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.models import Trial

    class Harness(BrowserHarness):
        info = BrowserHarnessCloud.info

        async def execute(self, _: object) -> dict[str, object]:
            return {"status": "done", "answer": _run_result(view, [])["answer"]}

    view = SimpleNamespace(
        status=RunStatus.completed, output={"answer": "$24", "currency": "USD"}, session_id=SESSION_ID
    )
    harness = Harness()
    harness._answer_schema = {"type": "object", "properties": {"answer": {}, "currency": {}}}
    await harness.start(Trial.model_construct(model=None, task_id="t", env={}), SimpleNamespace())
    output = await harness.turn(TurnInput(index=1, text="find the price", new_session=True))
    assert output.answer == '{"answer":"$24","currency":"USD"}'


def test_the_v3_cloud_api_claims_no_protected_login(monkeypatch) -> None:
    monkeypatch.setenv("PARALLAX_BROWSER_CLOUD_API", "v3")
    assert BrowserHarnessCloud().info.capabilities == frozenset({Capability.BROWSER})


def test_browser_capabilities_are_explicit_about_site_credentials() -> None:
    assert BrowserHarnessCloud.info.capabilities == frozenset({Capability.BROWSER, Capability.CREDENTIALS})
    for info in (BrowserHarnessOSS.info, BrowserHarnessRunner.info, CuaCodexHarness.info):
        assert info.capabilities == frozenset({Capability.BROWSER})
        assert Capability.CREDENTIALS not in info.capabilities
        assert Capability.AUTHORIZATION not in info.capabilities


def test_browser_workers_cannot_read_task_secret_environment(monkeypatch) -> None:
    from parallax.models import Trial

    monkeypatch.setenv("SHOP_PASSWORD", "correct-horse-battery-staple")
    harness = BrowserHarnessOSS()
    harness._task_secrets = {"shop_password": "SHOP_PASSWORD"}
    harness._trial = Trial.model_construct(env={"PARALLAX_WORLD_TOKEN": "world"})
    worker_env = harness._agent_env()
    assert worker_env.get("PARALLAX_WORLD_TOKEN") == "world"
    assert "SHOP_PASSWORD" not in worker_env


async def test_browser_evidence_replaces_secret_values_with_named_tags(monkeypatch) -> None:
    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.models import Area, TaskRecord, Trial
    from parallax.task import task_from_record

    class Harness(BrowserHarness):
        info = BrowserHarnessCloud.info

        async def execute(self, _: object) -> dict[str, object]:
            secret = "correct-horse-battery-staple"
            return {
                "status": "done",
                "answer": f"logged in with {secret}",
                "step_log": [{"action": "type", "value": secret}],
                "pages": [
                    {
                        "url": "https://shop.example/",
                        "title": "Login",
                        "text": secret,
                        "screenshot_png_b64": "iVBORw0KGgo=",
                    }
                ],
                "final_page": {
                    "url": "https://shop.example/",
                    "title": "Account",
                    "text": secret,
                    "screenshot_png_b64": "iVBORw0KGgo=",
                },
            }

    monkeypatch.setenv("SHOP_PASSWORD", "correct-horse-battery-staple")
    record = TaskRecord(
        id="browser-secret-scrub",
        version=1,
        area=Area.BROWSER,
        summary="scrub a login secret",
        prompt="Sign in and report the result.",
        grading="browser-evidence-v2",
        requires=frozenset({Capability.BROWSER, Capability.CREDENTIALS}),
        secrets={"shop_password": "SHOP_PASSWORD"},
        data={"start_url": "https://shop.example/"},
    )
    harness = Harness()
    task = task_from_record("fixture-source", record)
    harness.bind_task(task)
    await harness.start(Trial.model_construct(model=None, task_id=task.id, env={}), SimpleNamespace())
    output = await harness.turn(TurnInput(index=1, text="sign in", new_session=True))

    tag = "<secret>shop_password</secret>"
    assert output.answer == f"logged in with {tag}"
    evidence = harness.trial_metadata["browser"]
    assert isinstance(evidence, dict)
    assert tag in str(evidence) and "correct-horse-battery-staple" not in str(evidence)
    assert "iVBORw0KGgo=" not in str(evidence)


async def test_pages_a_run_read_without_navigating_reach_the_evidence_with_their_steps() -> None:
    """A runner that reads a page over HTTP leaves the browser where it was; the page it read is the evidence."""
    from parallax.browser_evidence import BrowserEvidence
    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.models import Trial

    class Harness(BrowserHarness):
        info = BrowserHarnessCloud.info

        async def execute(self, turn: SimpleNamespace) -> dict[str, object]:
            url = f"https://site.test/{turn.index}"
            return {
                "status": "done",
                "answer": "Heading",
                "step_log": [{"action": "read", "url": url, "page": 0}],
                "pages": [{"url": url, "title": "Frames", "text": f"Heading {turn.index}"}],
                "final_page": None,
            }

    harness = Harness()
    await harness.start(Trial.model_construct(model=None, task_id="t", env={}), SimpleNamespace())
    await harness.turn(TurnInput(index=1, text="read", new_session=True))
    await harness.turn(TurnInput(index=2, text="read again", new_session=False))

    evidence = BrowserEvidence.model_validate(harness.trial_metadata["browser"])
    assert [page.text for page in evidence.pages] == ["Heading 1", "Heading 2"]
    assert [(step.url, step.page) for step in evidence.steps] == [
        ("https://site.test/1", 0),
        ("https://site.test/2", 1),
    ]


async def test_v3_session_resolves_its_browser_for_evidence_and_costs() -> None:
    class Sessions:
        async def create(self, **_: object) -> SessionResponse:
            return v3_session(status=BuAgentSessionStatus.running)

        async def get(self, _: str) -> SessionResponse:
            return v3_session()

        async def stop(self, _: str) -> None:
            raise AssertionError("a stopped session should not be stopped again")

    class Browsers:
        def __init__(self) -> None:
            self.agent_session_ids: list[str] = []

        async def list(self, *, agent_session_id: str) -> BrowserSessionListResponse:
            self.agent_session_ids.append(agent_session_id)
            return v3_browser_list()

    class Client:
        def __init__(self) -> None:
            self.sessions = Sessions()
            self.browsers = Browsers()

    client = Client()
    result = await _run_v3(client, "find it", "provider/model")
    assert client.browsers.agent_session_ids == [str(SESSION_ID)]
    assert result["browser_id"] == str(BROWSER_ID)
    assert result["live_url"] == "https://live.test/session"
    # The live view URL is the provider's viewer, not the page the agent finished on.
    assert result["url"] is None
    assert result["cost_lines"] == [
        {"component": "model", "dollars": "0.11"},
        {"component": "browser", "dollars": "0.03"},
        {"component": "proxy", "dollars": "0.02"},
    ]


def test_unknown_cost_components_are_kept_for_row_settlement() -> None:
    from parallax.harnesses.browser.harnesses import _known_costs

    assert _known_costs(
        [
            {"component": "browser", "dollars": 0.12},
            {"component": "proxy", "dollars": None},
            {"component": "model", "dollars": "n/a"},
        ]
    ) == {"browser": 0.12, "proxy": None, "model": None}


def test_a_component_billed_in_several_lines_costs_their_sum() -> None:
    from parallax.harnesses.browser.harnesses import _known_costs

    assert _known_costs(
        [
            {"component": "decision", "dollars": 0.01},
            {"component": "decision", "dollars": 0.02},
            {"component": "llm", "dollars": 0.1},
            {"component": "llm", "dollars": None},
        ]
    ) == {"decision": 0.03, "llm": None}


def test_oss_declares_the_pinned_runtime_install() -> None:
    from parallax.harnesses.browser import pins

    assert f"browser-use=={pins.load()['browser-use']['version']}" in BrowserHarnessOSS.info.install


def test_ultrafast_declares_the_pinned_1_0_runtime_install() -> None:
    from parallax.harnesses.browser import pins

    assert pins.load()["browser-use-1"]["version"] == "1.0.8"
    assert "browser-use==1.0.8" in BrowserHarnessUltrafast.info.install
    assert BrowserHarnessUltrafast.info.name == "browser-use-1-ultrafast"
    assert BrowserHarnessUltrafast.info.capabilities == frozenset({Capability.BROWSER})
    assert BrowserHarnessUltrafast.info.credential_env == ("BROWSER_USE_API_KEY",)
    assert BrowserHarnessUltrafast.pin == "browser-use-1"
    assert BrowserHarnessUltrafast.fixed_model == "openai/gpt-6-luna"


def test_ultrafast_marks_a_row_that_names_another_model_invalid() -> None:
    from parallax.models import Trial

    harness = BrowserHarnessUltrafast()
    harness._trial = Trial.model_construct(model="openai/other", env={})
    request = harness._worker_request(TurnInput(index=1, text="find it", new_session=True))
    assert request["model"] == "openai/gpt-6-luna"
    assert harness.trial_invalid == ["asked for model openai/other, this arm runs openai/gpt-6-luna"]


async def test_ultrafast_sends_secrets_only_as_sensitive_data(monkeypatch) -> None:
    from pathlib import Path

    from parallax.harnesses.browser import harnesses
    from parallax.models import Trial

    sent: list[dict[str, object]] = []

    class Worker:
        def __init__(self, command: object, *, env: object, cwd: Path) -> None:
            self.events = iter(
                [
                    {"event": "ready"},
                    {"event": "result", "status": "completed", "answer": "ok"},
                    {"event": "stopped", "stopped": True},
                ]
            )

        async def __aenter__(self) -> Worker:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def send(self, message: dict[str, object]) -> None:
            sent.append(message)

        async def receive(self, event: str) -> dict[str, object]:
            return next(self.events)

    monkeypatch.setattr(harnesses, "JsonWorker", Worker)
    harness = BrowserHarnessUltrafast()
    harness._trial = Trial.model_construct(model="openai/gpt-6-luna", env={})
    harness._secret_values = {"shop_password": "correct-horse-battery-staple"}
    result = await harness.execute(TurnInput(index=1, text="sign in", new_session=True))
    assert result["status"] == "completed"
    assert sent[0]["model"] == "openai/gpt-6-luna"
    assert sent[0]["sensitive_data"] == {"shop_password": "correct-horse-battery-staple"}
    assert harness.trial_details()["mode"] == "ultrafast"
    assert harness.trial_details()["reasoning_effort"] == "low"
    await harness.stop()


def test_browser_worker_does_not_declare_a_metered_provider_as_native() -> None:
    from parallax.harnesses.base import credential_env_problems, native_credentials
    from parallax.harnesses.browser.harnesses import BrowserHarnessRunner

    assert credential_env_problems(BrowserHarnessRunner.info) == []
    assert native_credentials(
        BrowserHarnessRunner.info, {"BROWSER_USE_API_KEY": "browser", "OPENROUTER_API_KEY": "model"}
    ) == {"BROWSER_USE_API_KEY": "browser"}


async def test_browser_timing_starts_before_execute_finishes() -> None:
    import asyncio

    from parallax.harnesses.browser.clock import AttemptClock
    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.models import Trial

    class Harness(BrowserHarness):
        info = BrowserHarnessOSS.info

        async def execute(self, _: object) -> dict[str, object]:
            await asyncio.sleep(0.01)
            return {"status": "done", "answer": "ok", "ready": True}

    harness = Harness()
    harness._trial = Trial.model_construct(model=None)
    harness._clock = AttemptClock(bounded=False)
    await harness.turn(TurnInput(index=1, text="find it", new_session=True))
    assert harness.trial_timing().breakdown["agent"] >= 5


async def test_a_turn_reports_every_model_the_harness_ran() -> None:
    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.models import Trial

    class Harness(BrowserHarness):
        info = BrowserHarnessOSS.info

        async def execute(self, _: object) -> dict[str, object]:
            return {"status": "done", "answer": "ok", "actual_models": ["openai/gpt-6-luna", "classifier-1"]}

    harness = Harness()
    harness._trial = Trial.model_construct(model=None)
    out = await harness.turn(TurnInput(index=1, text="find it", new_session=True))
    assert out.actual_models == ("openai/gpt-6-luna", "classifier-1")


async def test_a_reattached_browser_does_not_restart_the_turns_agent_time() -> None:
    import asyncio

    from parallax.harnesses.browser.clock import AttemptClock
    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.models import Trial

    class Harness(BrowserHarness):
        info = BrowserHarnessOSS.info

        async def execute(self, _: object) -> dict[str, object]:
            self._agent_started()  # browser.ready
            await asyncio.sleep(0.1)
            self._agent_started()  # browser.reattached
            await asyncio.sleep(0.01)
            self._agent_finished()
            return {"status": "done", "answer": "ok", "ready": True}

    harness = Harness()
    harness._trial = Trial.model_construct(model=None)
    harness._clock = AttemptClock(bounded=False)
    out = await harness.turn(TurnInput(index=1, text="find it", new_session=True))
    assert out.latency_ms is not None and out.latency_ms >= 100


async def test_agent_time_ends_when_the_agent_answers_not_after_cleanup() -> None:
    import asyncio

    from parallax.harnesses.browser.clock import AttemptClock
    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.models import Trial

    class Harness(BrowserHarness):
        info = BrowserHarnessOSS.info

        async def execute(self, _: object) -> dict[str, object]:
            await asyncio.sleep(0.01)
            self._agent_finished()
            await asyncio.sleep(0.2)  # reading the final page, stopping the browser, settling its charges
            return {"status": "done", "answer": "ok", "ready": True}

    harness = Harness()
    harness._trial = Trial.model_construct(model=None)
    harness._clock = AttemptClock(bounded=False)
    out = await harness.turn(TurnInput(index=1, text="find it", new_session=True))
    assert 5 <= harness.trial_timing().breakdown["agent"] < 150
    # The runner's agent time is the turn's latency, so it must stop at the answer too.
    assert out.latency_ms is not None and 5 <= out.latency_ms < 150


def _browser_task_harness(execute):
    from parallax.harnesses.browser.clock import AttemptClock
    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.models import Area, Capability, Maturity, Provenance, TaskRecord
    from parallax.task import task_from_record

    class Harness(BrowserHarness):
        info = BrowserHarnessOSS.info
        executed = False

        async def execute(self, _: object) -> dict[str, object]:
            self.executed = True
            return execute()

    record = TaskRecord(
        id="browser-site",
        version=1,
        area=Area.BROWSER,
        summary="a site",
        prompt="visit the site",
        grading="browser-evidence-v2",
        requires=frozenset({Capability.BROWSER}),
        maturity=Maturity.REGRESSION,
        provenance=Provenance.CONVERTED,
        data={"website": "https://site.test/start"},
    )
    harness = Harness()
    harness.bind_task(task_from_record("fixture-source", record))
    harness._trial = SimpleNamespace(model=None)
    harness._clock = AttemptClock(bounded=False)
    return harness


async def test_a_site_that_refuses_plain_http_clients_still_gets_its_browser(monkeypatch) -> None:
    # Sites behind bot protection refuse a plain HTTP request from the host and load in the trial's browser, so the
    # host is never asked whether the site is up.
    async def refused(*_args, **_kwargs):
        raise httpx.ReadTimeout("refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", refused)
    harness = _browser_task_harness(lambda: {"status": "done", "answer": "ok"})
    output = await harness.turn(TurnInput(index=1, text="visit", new_session=True))
    assert harness.executed
    assert output.error is None


async def test_an_outage_the_browser_reports_is_an_environment_interruption() -> None:
    harness = _browser_task_harness(
        lambda: {"status": "unavailable", "answer": None, "error": "site unavailable: https://site.test/start"}
    )
    output = await harness.turn(TurnInput(index=1, text="visit", new_session=True))
    assert output.error == "environment interruption: site unavailable: https://site.test/start"
    assert harness.trial_costs() == {"model": 0.0}


def test_v4_steps_are_the_finished_tool_calls_of_the_event_stream() -> None:
    """Shapes recorded from a live V4 run: step boundaries and the `running` echo of a call are not steps."""
    code = "await page.goto('https://site.test'); await bu.state();"

    def tool(status: str, **state: object) -> dict[str, object]:
        part = {"tool": "javascript", "type": "tool", "state": {"input": {"code": code}, "status": status, **state}}
        return {"type": "core.event", "data": {"part": part, "type": "tool_use"}}

    events = [
        {"type": "run.created", "data": {}},
        {"type": "core.event", "data": {"part": {"type": "step-start"}, "type": "step_start"}},
        tool("running"),
        tool("completed", output="[state] Site | https://site.test/"),
        tool("error", error="TimeoutError"),
        {"type": "core.event", "data": {"part": {"type": "step-finish"}, "type": "step_finish"}},
        {"type": "llm.response", "data": {}},
    ]
    steps = _run_result(SimpleNamespace(status="completed", output="Site"), events)["step_log"]
    assert [(step["action"], step["ok"], step.get("quote")) for step in steps] == [
        ("javascript", True, "[state] Site | https://site.test/"),
        ("javascript", False, "TimeoutError"),
    ]
    assert steps[0]["value"] == code


async def test_every_open_tab_is_evidence_when_no_final_page_is_singled_out(monkeypatch) -> None:
    """A run that opened a new tab ends with two pages; both reach the judge instead of neither."""
    from parallax.browser_evidence import BrowserEvidence
    from parallax.harnesses.browser import harnesses
    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.harnesses.browser.observer import AmbiguousFinalPage
    from parallax.models import Trial

    tabs = [
        {"url": "https://site.test/link", "title": "Link", "text": "Open the new tab"},
        {"url": "https://site.test/new", "title": "New", "text": "I am a new page"},
    ]

    class Observer:
        async def observe(self, cdp_url: str, *, url: str | None = None) -> dict[str, object]:
            raise AmbiguousFinalPage(len(tabs), tabs)

    monkeypatch.setattr(harnesses, "load_observer", lambda name: Observer())

    class Harness(BrowserHarness):
        info = BrowserHarnessCloud.info

        async def execute(self, turn: SimpleNamespace) -> dict[str, object]:
            if turn.index == 1:
                return {"status": "done", "answer": "Link", "final_page": tabs[0]}
            return {"status": "done", "answer": "I am a new page", "final_page": await self.observe("ws://cdp")}

    harness = Harness()
    await harness.start(Trial.model_construct(model=None, task_id="t", env={}), SimpleNamespace())
    await harness.turn(TurnInput(index=1, text="open the page", new_session=True))
    await harness.turn(TurnInput(index=2, text="follow the link", new_session=False))

    evidence = BrowserEvidence.model_validate(harness.trial_metadata["browser"])
    assert evidence.final_page is None
    assert [page.url for page in evidence.pages] == [tab["url"] for tab in tabs]
    assert evidence.observed()
    assert harness.trial_notes == ["final page not singled out: 2 tabs open, each kept as a page"]


async def test_a_host_worker_runs_in_a_host_directory_even_when_the_box_is_docker(monkeypatch) -> None:
    """A Docker box's home is a container path; a worker spawned on the host must not run there."""
    from pathlib import Path

    from parallax.harnesses.browser import harnesses
    from parallax.models import Trial

    seen: list[Path] = []

    class Worker:
        def __init__(self, command: object, *, env: object, cwd: Path) -> None:
            seen.append(cwd)
            assert cwd.is_dir()

        async def __aenter__(self) -> Worker:
            raise RuntimeError("stop after spawn")

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(harnesses, "JsonWorker", Worker)
    harness = BrowserHarnessOSS()
    harness._trial = Trial.model_construct(model="bu-fast", home=Path("/home/agent"), env={})
    with pytest.raises(RuntimeError, match="stop after spawn"):
        await harness.execute(TurnInput(index=1, text="find it", new_session=True))
    assert seen and seen[0] != Path("/home/agent")
    await harness.stop()
    assert not seen[0].exists()


async def test_a_worker_that_fails_to_start_costs_nothing(monkeypatch) -> None:
    """A trial whose agent never started spent nothing, so its budget hold settles at zero, not the reservation."""
    from pathlib import Path

    from parallax.harnesses.browser import harnesses
    from parallax.harnesses.browser.clock import AttemptClock
    from parallax.models import Trial

    class Worker:
        def __init__(self, command: object, *, env: object, cwd: Path) -> None:
            raise FileNotFoundError(2, "No such file or directory", "/home/agent")

    monkeypatch.setattr(harnesses, "JsonWorker", Worker)
    harness = BrowserHarnessOSS()
    harness._trial = Trial.model_construct(model="bu-fast", home=Path("/home/agent"), env={})
    harness._clock = AttemptClock(bounded=False)
    out = await harness.turn(TurnInput(index=1, text="find it", new_session=True))
    assert "FileNotFoundError" in str(out)
    assert harness.trial_costs() == {"model": 0.0}
    await harness.stop()


def test_oss_runs_in_this_python_only_with_the_pinned_browser_use(monkeypatch) -> None:
    import importlib.metadata
    import sys

    from parallax.harnesses.base import HarnessError
    from parallax.harnesses.browser import harnesses, pins

    pinned = pins.load()["browser-use"]["version"]
    found: dict[str, str] = {}

    def version(name: str) -> str:
        if name not in found:
            raise importlib.metadata.PackageNotFoundError(name)
        return found[name]

    monkeypatch.setattr(importlib.metadata, "version", version)
    monkeypatch.setattr(harnesses.shutil, "which", lambda name: "/usr/bin/uv")
    assert harnesses.oss_python()[-3:] == ("--with", f"browser-use=={pinned}", "python")
    found["browser-use"] = "0.9.0"
    assert harnesses.oss_python()[0] == "/usr/bin/uv"
    found["browser-use"] = pinned
    assert harnesses.oss_python() == (sys.executable,)
    del found["browser-use"]
    monkeypatch.setattr(harnesses.shutil, "which", lambda name: None)
    with pytest.raises(HarnessError, match="needs browser-use"):
        harnesses.oss_python()


async def test_oss_reports_the_browser_use_its_worker_runs(monkeypatch) -> None:
    import sys

    from parallax.harnesses.base import HarnessError
    from parallax.harnesses.browser import harnesses

    # A stand-in interpreter: its own -c runs, and the probe's arguments follow as its argv.
    monkeypatch.setattr(harnesses, "oss_python", lambda *_: (sys.executable, "-c", "print('1.0.5')", "--"))
    assert await BrowserHarnessOSS().version(None) == "browser-use 1.0.5"
    monkeypatch.setattr(harnesses, "oss_python", lambda *_: (sys.executable, "-c", "raise SystemExit('no uv env')", "--"))
    with pytest.raises(HarnessError, match="no uv env"):
        await BrowserHarnessOSS().version(None)


def test_a_worker_error_before_ready_names_its_cause() -> None:
    from parallax.harnesses.browser.subprocess import WorkerEventError

    error = WorkerEventError(
        "ready", {"event": "result", "error": "ModuleNotFoundError: No module named 'browser_use'"}
    )
    assert "No module named 'browser_use'" in str(error)


async def test_a_worker_that_fails_after_its_browser_opened_keeps_the_browser_cost(monkeypatch) -> None:
    import contextlib
    from pathlib import Path

    from parallax.harnesses.browser import harnesses
    from parallax.harnesses.browser.clock import AttemptClock
    from parallax.models import Trial

    browser = SimpleNamespace(
        cdp_url="wss://cdp.test", open_seconds=1.0, cost=(), browser_id="browser-1", live_url="https://live.test/1"
    )

    @contextlib.asynccontextmanager
    async def paid_browser(http: object, *, screen: object, minutes: int):  # noqa: ANN202
        try:
            yield browser
        finally:
            browser.cost = ({"component": "browser", "dollars": 0.02},)

    class Worker:
        def __init__(self, command: object, *, env: object, cwd: Path) -> None:
            raise FileNotFoundError(2, "No such file or directory", "missing-worker")

    monkeypatch.setattr(harnesses, "cloud_browser", paid_browser)
    monkeypatch.setattr(harnesses, "JsonWorker", Worker)
    harness = BrowserHarnessRunner()
    harness._trial = Trial.model_construct(model="m", home=Path("/home/agent"), env={})
    harness._clock = AttemptClock(bounded=False)
    await harness.turn(TurnInput(index=1, text="find it", new_session=True))
    assert harness.trial_costs() == {"browser": 0.02}
    await harness.stop()


async def test_a_site_down_in_the_runners_own_browser_is_an_interruption_before_the_agent(monkeypatch) -> None:
    import contextlib
    from pathlib import Path

    from parallax.harnesses.browser import harnesses
    from parallax.harnesses.browser.clock import AttemptClock
    from parallax.models import Trial

    browser = SimpleNamespace(
        cdp_url="wss://cdp.test", open_seconds=1.0, cost=(), browser_id="browser-1", live_url="https://live.test/1"
    )

    @contextlib.asynccontextmanager
    async def paid_browser(http: object, *, screen: object, minutes: int):  # noqa: ANN202
        try:
            yield browser
        finally:
            browser.cost = ({"component": "browser", "dollars": 0.02},)

    asked: list[tuple[str, str]] = []

    async def down(cdp_url: str, url: str) -> str:
        asked.append((cdp_url, url))
        return f"site unavailable: {url} unreachable in the trial's browser (net::ERR_CONNECTION_REFUSED)"

    class Worker:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("the agent started on a site that is down")

    monkeypatch.setattr(harnesses, "cloud_browser", paid_browser)
    monkeypatch.setattr(harnesses, "browser_site_down", down)
    monkeypatch.setattr(harnesses, "JsonWorker", Worker)
    harness = BrowserHarnessRunner()
    harness._start_url = "https://site.test/start"
    harness._trial = Trial.model_construct(model="m", home=Path("/home/agent"), env={})
    harness._clock = AttemptClock(bounded=False)
    output = await harness.turn(TurnInput(index=1, text="find it", new_session=True))
    assert asked == [("wss://cdp.test", "https://site.test/start")]
    assert output.error == (
        "environment interruption: site unavailable: https://site.test/start unreachable in the trial's browser "
        "(net::ERR_CONNECTION_REFUSED)"
    )
    assert harness.trial_costs() == {"browser": 0.02}
    await harness.stop()


async def test_a_browser_harness_trial_costs_its_agent_model_as_well_as_its_browser(monkeypatch, tmp_path) -> None:
    import contextlib
    import sys

    from parallax.harnesses.browser import harnesses
    from parallax.harnesses.browser.clock import AttemptClock
    from parallax.harnesses.browser.toolchain import Toolchain
    from parallax.models import Trial

    browser = SimpleNamespace(
        cdp_url="wss://cdp.test", open_seconds=1.0, cost=(), browser_id="browser-1", live_url="https://live.test/1"
    )

    @contextlib.asynccontextmanager
    async def paid_browser(http: object, *, screen: object, minutes: int):  # noqa: ANN202
        try:
            yield browser
        finally:
            browser.cost = ({"component": "browser", "dollars": 0.02},)

    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json,sys; request=json.loads(sys.stdin.readline()); "
        "print(json.dumps({'event':'result','status':'done','answer':request['agent'],"
        "'cost_lines':[{'component':'agent model','dollars':0.003}]}),flush=True); "
        "sys.stdin.readline(); print(json.dumps({'event':'stopped','stopped':True}),flush=True)"
    )
    monkeypatch.setattr(harnesses, "cloud_browser", paid_browser)
    monkeypatch.setenv("PARALLAX_BROWSER_HARNESS_COMMAND", f"{sys.executable} {worker}")

    class Observed(harnesses.BrowserHarnessClaudeCode):
        async def observe(self, cdp_url: str, *, url: str | None = None) -> dict[str, object]:
            return {"url": "https://example.test/"}

    harness = Observed()
    harness._toolchain = Toolchain(agent_bin=tmp_path, bin_dir=tmp_path, skill=tmp_path, version="v")
    harness._trial = Trial.model_construct(model="anthropic/claude-haiku-5.5", home=tmp_path, env={})
    harness._clock = AttemptClock(bounded=False)
    output = await harness.turn(TurnInput(index=1, text="find it", new_session=True))
    assert (output.error, output.answer) == (None, "claude-code")
    assert harness.trial_costs() == {"agent model": 0.003, "browser": 0.02}
    await harness.stop()


async def test_a_v4_run_that_was_created_is_spending_even_if_polling_fails() -> None:
    from parallax.harnesses.browser import harnesses

    class Runs:
        async def create(self, goal: str, **_: object) -> SimpleNamespace:
            return SimpleNamespace(id="run-1")

        async def status(self, run_id: str) -> None:
            raise RuntimeError("status unavailable")

        async def cancel(self, run_id: str) -> None:
            return None

        async def stop(self, run_id: str) -> None:
            return None

    spent: list[bool] = []
    with pytest.raises(Exception):  # noqa: B017, PT011 - the polling failure itself is not under test
        await harnesses._run_v4(SimpleNamespace(runs=Runs()), "find it", None, 0.0, spending=lambda: spent.append(True))
    assert spent == [True]


async def test_oss_probes_its_runtime_as_the_worker_runs_it(monkeypatch) -> None:
    import sys

    from parallax.harnesses.browser import harnesses

    seen: list[tuple[str, ...]] = []
    real = harnesses.asyncio.create_subprocess_exec

    async def record(*argv: str, **kwargs: object):  # noqa: ANN202
        seen.append(argv)
        return await real(sys.executable, "-c", "print('1.0.5')", **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(harnesses, "oss_python", lambda *_: ("py",))
    monkeypatch.setattr(harnesses.asyncio, "create_subprocess_exec", record)
    assert await BrowserHarnessOSS().version(None) == "browser-use 1.0.5"
    assert seen[0][:3] == ("py", "-I", "-c") and "import browser_use" in seen[0][3]


async def test_a_later_turn_that_spent_without_a_price_makes_the_trial_cost_unknown(monkeypatch) -> None:
    from pathlib import Path

    from parallax.harnesses.browser.clock import AttemptClock
    from parallax.models import Trial

    class TwoTurns(BrowserHarnessRunner):
        async def execute(self, turn: object) -> dict[str, object]:
            self._spending()
            if turn.index == 1:  # ty: ignore[unresolved-attribute]
                return {"status": "completed", "answer": "a", "cost_lines": [{"component": "model", "dollars": 0.01}]}
            raise RuntimeError("polling failed after the run was created")

    harness = TwoTurns()
    harness._trial = Trial.model_construct(model="m", home=Path("/home/agent"), env={})
    harness._clock = AttemptClock(bounded=False)
    await harness.turn(TurnInput(index=1, text="first", new_session=True))
    assert harness.trial_costs() == {"model": 0.01}
    await harness.turn(TurnInput(index=2, text="second", new_session=False))
    assert None in harness.trial_costs().values()
    await harness.stop()


async def test_a_later_turn_that_reports_no_cost_lines_makes_the_trial_cost_unknown() -> None:
    from pathlib import Path

    from parallax.harnesses.browser.clock import AttemptClock
    from parallax.models import Trial

    class TwoTurns(BrowserHarnessRunner):
        async def execute(self, turn: object) -> dict[str, object]:
            self._spending()
            lines = [{"component": "model", "dollars": 0.01}] if turn.index == 1 else []  # ty: ignore[unresolved-attribute]
            return {"status": "completed", "answer": "a", "cost_lines": lines}

    harness = TwoTurns()
    harness._trial = Trial.model_construct(model="m", home=Path("/home/agent"), env={})
    harness._clock = AttemptClock(bounded=False)
    await harness.turn(TurnInput(index=1, text="first", new_session=True))
    await harness.turn(TurnInput(index=2, text="second", new_session=False))
    assert None in harness.trial_costs().values()
    await harness.stop()


async def test_a_turn_cancelled_at_its_limit_keeps_the_pages_and_steps_the_agent_reached() -> None:
    """2026-11-17: a browser agent turn stuck in a wait was cancelled at the 1800 s limit, and the trial reported no
    page or step, so the judge could not attribute the timeout and the trial was graded invalid and retried."""
    import asyncio

    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.models import Trial

    page = {"url": "https://practice.test/dynamic-loading/2", "title": "Dynamic Loading 2", "text": "Start"}

    class Harness(BrowserHarness):
        info = BrowserHarnessOSS.info

        async def execute(self, _: object) -> dict[str, object]:
            await asyncio.sleep(3600)  # a wait on a page that never changes
            return {"status": "done", "answer": "ok"}

        def cancelled(self) -> dict[str, object]:
            return {"pages": [page], "step_log": [{"action": "wait (in progress)", "url": page["url"], "page": 0}]}

    harness = Harness()
    harness._trial = Trial.model_construct(model=None)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await harness.turn(TurnInput(index=1, text="find it", new_session=True))
    browser = harness.trial_metadata["browser"]
    assert browser["pages"] == [page]
    assert browser["steps"] == [{"action": "wait (in progress)", "url": page["url"], "page": 0}]


async def test_a_turn_tells_the_harness_how_long_it_has_left() -> None:
    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.models import Trial, TurnInput

    left: list[float | None] = []

    class Harness(BrowserHarness):
        info = BrowserHarnessOSS.info

        async def execute(self, _: object) -> dict[str, object]:
            left.append(self.seconds_left())
            return {"status": "done", "answer": "ok"}

    harness = Harness()
    harness._trial = Trial.model_construct(model=None)
    await harness.turn(TurnInput(index=1, text="find it", new_session=True, deadline_s=1800))
    await harness.turn(TurnInput(index=2, text="and this", new_session=False))
    assert left[0] is not None and 1790 < left[0] <= 1800
    assert left[1] is None


def test_agent_browser_reaches_the_trials_browser_from_every_session_and_browser_harness_by_its_own_variable() -> None:
    from parallax.harnesses.browser.harnesses import AgentBrowserRunner
    from parallax.harnesses.browser.toolchain import Cli

    assert AgentBrowserRunner.info.name == "agent-browser"
    assert AgentBrowserRunner.cli is Cli.AGENT_BROWSER and BrowserHarnessRunner.cli is Cli.BROWSER_HARNESS
    assert Cli.AGENT_BROWSER.environment("wss://cdp.test", "t1") == {
        "AGENT_BROWSER_CDP": "wss://cdp.test",
        "AGENT_BROWSER_NAMESPACE": "t1",
    }
    assert Cli.AGENT_BROWSER.detach() == ["agent-browser", "close", "--all"]
    # Its daemon is per BU_NAME, so trials side by side on one runner never share one.
    assert Cli.BROWSER_HARNESS.environment("wss://cdp.test", "t1") == {"BU_CDP_WS": "wss://cdp.test", "BU_NAME": "t1"}
    assert Cli.BROWSER_HARNESS.detach() == []


async def test_every_cloud_arm_records_its_browser_and_live_view(monkeypatch, tmp_path) -> None:
    """A Browser Use cloud browser's live view link is gone once it stops, so each arm keeps it beside the id."""
    import contextlib
    from pathlib import Path

    from parallax.harnesses.browser import harnesses
    from parallax.harnesses.browser.clock import AttemptClock
    from parallax.models import Trial

    async def no_cost(browser_id: str, *, client: object = None) -> tuple[()]:
        return ()

    monkeypatch.setattr(harnesses, "stopped_browser_cost", no_cost)

    # The V4 cloud arm: read when the run is over, while its browser is still open.
    view = SimpleNamespace(live_url="https://live.test/v4", cdp_url=None)
    client = SimpleNamespace(browsers=SimpleNamespace(get=lambda _: _ready(view)))
    cloud_arm = BrowserHarnessCloud()
    await cloud_arm._observe_cloud_browser(client, "browser-v4", {"url": "https://site.test"})
    # A V3 run gives its link when the session is created, and that one is kept.
    await cloud_arm._observe_cloud_browser(client, "browser-v3", {"live_url": "https://live.test/v3"})
    assert cloud_arm.trial_details()["browsers"] == [
        {"browser_id": "browser-v4", "live_url": "https://live.test/v4"},
        {"browser_id": "browser-v3", "live_url": "https://live.test/v3"},
    ]

    # The open-source arm: its worker opens the browser and reports the link when it is ready.
    requests: list[dict[str, object]] = []

    class Worker:
        def __init__(self, command: object, *, env: object, cwd: Path) -> None:
            self.events = {
                "ready": {"cdp_url": None, "browser_id": "browser-oss", "live_url": "https://live.test/oss"},
                "result": {"status": "done", "answer": "ok"},
                "stopped": {"stopped": True},
            }

        async def __aenter__(self) -> Worker:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def send(self, message: dict[str, object]) -> None:
            requests.append(message)

        async def receive(self, event: str) -> dict[str, object]:
            return dict(self.events[event])

    monkeypatch.setattr(harnesses, "JsonWorker", Worker)
    oss = BrowserHarnessOSS()
    oss._trial = Trial.model_construct(model="bu-fast", home=Path("/home/agent"), env={})
    oss._clock = AttemptClock(bounded=False)
    await oss.turn(TurnInput(index=1, text="find it", new_session=True, deadline_s=600))
    assert oss.trial_details()["browsers"] == [{"browser_id": "browser-oss", "live_url": "https://live.test/oss"}]
    assert requests[0]["browser_minutes"] == 10
    await oss.stop()

    # browser-harness, agent-browser and browser-harness-claude-code: the browser Parallax opens for the worker.
    asked: list[int] = []
    browser = SimpleNamespace(
        cdp_url="wss://cdp.test", open_seconds=1.0, cost=(), browser_id="browser-bh", live_url="https://live.test/bh"
    )

    @contextlib.asynccontextmanager
    async def paid_browser(http: object, *, screen: object, minutes: int):  # noqa: ANN202
        asked.append(minutes)
        yield browser

    class Failing:
        def __init__(self, command: object, *, env: object, cwd: Path) -> None:
            raise FileNotFoundError(2, "No such file or directory", "missing-worker")

    monkeypatch.setattr(harnesses, "cloud_browser", paid_browser)
    monkeypatch.setattr(harnesses, "JsonWorker", Failing)
    runner_arm = BrowserHarnessRunner()
    runner_arm._toolchain = SimpleNamespace(request=dict)  # type: ignore[assignment]
    runner_arm._trial = Trial.model_construct(model="m", home=tmp_path, env={})
    runner_arm._clock = AttemptClock(bounded=False)
    await runner_arm.turn(TurnInput(index=1, text="find it", new_session=True, deadline_s=90))
    assert runner_arm.trial_details()["browsers"] == [{"browser_id": "browser-bh", "live_url": "https://live.test/bh"}]
    assert asked == [2]
    await runner_arm.stop()


async def _ready(value: object) -> object:
    return value


async def test_a_v4_run_cancelled_while_its_page_is_read_still_stops_its_browser(monkeypatch) -> None:
    """The run is over but its browser is open: a turn cancelled while reading the final page must still stop it."""
    import asyncio

    import browser_use_sdk.v4
    from parallax.harnesses.browser import harnesses
    from parallax.models import Trial

    stopped: list[str] = []

    class Browsers:
        async def get(self, _: str) -> object:
            raise asyncio.CancelledError

    class Client:
        def __init__(self, **_: object) -> None:
            self.browsers = Browsers()

        async def close(self) -> None:
            return None

    async def run(*_: object, **__: object) -> dict[str, object]:
        return {"status": "done", "answer": "ok", "browser_id": "browser-1", "actual_model": None}

    async def stop(browser_id: str, *, client: object = None) -> tuple[()]:
        stopped.append(browser_id)
        return ()

    monkeypatch.setattr(browser_use_sdk.v4, "AsyncBrowserUse", Client)
    monkeypatch.setattr(harnesses, "_run_v4", run)
    monkeypatch.setattr(harnesses, "stopped_browser_cost", stop)
    harness = BrowserHarnessCloud()
    harness._trial = Trial.model_construct(model=None, task_id="t", env={})
    with pytest.raises(asyncio.CancelledError):
        await harness.execute(TurnInput(index=1, text="find it", new_session=True))
    assert stopped == ["browser-1"]
    assert harness.trial_details()["browsers"] == [{"browser_id": "browser-1", "live_url": None}]
