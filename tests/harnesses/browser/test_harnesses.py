from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

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
    BrowserHarnessOSS,
    _evidence_payload,
    _interruption,
    _run_v3,
    _run_v4,
)

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
        citations=[{"url": "https://site.test/", "quote": "Complete"}],
    )
    assert evidence["final_page"] == {"url": "https://site.test/", "title": "Done", "text": "Complete"}
    assert evidence["steps"] == [{"url": "https://site.test/", "action": "click"}]


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
            {"component": "jev", "dollars": 0.01},
            {"component": "jev", "dollars": 0.02},
            {"component": "llm", "dollars": 0.1},
            {"component": "llm", "dollars": None},
        ]
    ) == {"jev": 0.03, "llm": None}


def test_oss_declares_the_pinned_runtime_install() -> None:
    from parallax.harnesses.browser import pins

    assert f"browser-use=={pins.load()['browser-use']['version']}" in BrowserHarnessOSS.info.install


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
    await harness.turn(SimpleNamespace(index=1, text="find it"))
    assert harness.trial_timing().breakdown["agent"] >= 5


async def test_browser_site_preflight_is_an_environment_interruption(monkeypatch) -> None:
    from parallax.harnesses.browser.harnesses import BrowserHarness
    from parallax.models import Area, Capability, Maturity, Provenance, TaskRecord
    from parallax.task import task_from_record

    class Harness(BrowserHarness):
        info = BrowserHarnessOSS.info
        executed = False

        async def execute(self, _: object) -> dict[str, object]:
            self.executed = True
            return {"status": "done", "answer": "ok"}

    record = TaskRecord(
        id="browser-preflight",
        version=1,
        area=Area.BROWSER,
        summary="preflight",
        prompt="visit the site",
        grading="browser-evidence-v1",
        requires=frozenset({Capability.BROWSER}),
        maturity=Maturity.REGRESSION,
        provenance=Provenance.CONVERTED,
        data={"website": "https://site.test/start"},
    )
    task_from_record("fixture-source", record)
    harness = Harness()
    harness.bind_task(task_from_record("fixture-source", record))
    monkeypatch.setattr(
        "parallax.harnesses.browser.harnesses.site_down",
        lambda *_args, **_kwargs: _unavailable(),
    )
    harness._trial = SimpleNamespace(model=None)
    from parallax.harnesses.browser.clock import AttemptClock

    harness._clock = AttemptClock(bounded=False)
    output = await harness.turn(SimpleNamespace(index=1, text="visit"))
    assert output.error == "environment interruption: site unavailable"
    assert not harness.executed


async def _unavailable() -> str:
    return "site unavailable"
