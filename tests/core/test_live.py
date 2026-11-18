"""The live feed: what the runner sends as trials start and finish, and the progress a watcher reads back."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from parallax import runner
from parallax.models import Isolation, RunManifest, Verdict
from parallax.runner import RunOptions, read_results, run_async
from parallax.task import discover
from parallax.track import live
from parallax.track.live import FINISHED_EVENT, RUN_EVENT, STARTED_EVENT, Event, LiveFeed, progress, runs

TASKS = Path(__file__).parent / "fixtures" / "tasks"
T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self.flushed = 0

    def create_event(self, *, name: str, metadata: dict[str, str]) -> None:
        if self.fail:
            raise RuntimeError("langfuse down")
        self.sent.append((name, json.loads(metadata[live.PAYLOAD])))

    def flush(self) -> None:
        self.flushed += 1


def event(name: str, minutes: float, **payload: Any) -> Event:
    at = T0 + timedelta(minutes=minutes)
    return Event(name=name, session="run-1", at=at, payload={"at": at.isoformat(), **payload})


def finished(minutes: float, key: str, verdict: str, *, label: str = "a", attempt: int = 0, **extra: Any) -> Event:
    return event(FINISHED_EVENT, minutes, key=key, task=key, label=label, repeat=0, attempt=attempt, verdict=verdict,
                 **extra)  # fmt: skip


def test_progress_counts_latest_attempts_and_projects_an_eta() -> None:
    events = [
        event(RUN_EVENT, 0, state="running", total=10, already_done=0, concurrency=2, labels=["a", "b"]),
        event(STARTED_EVENT, 0, key="t1", task="t1", label="a", repeat=0, attempt=0),
        finished(1, "t1", "invalid", reason="site down", cost_usd=0.005),
        event(STARTED_EVENT, 1, key="t1", task="t1", label="a", repeat=0, attempt=1),
        finished(2, "t1", "pass", attempt=1, wall_s=30.0, cost_usd=0.01),
        event(STARTED_EVENT, 2, key="t2", task="t2", label="b", repeat=0, attempt=0),
        finished(3, "t2", "fail", label="b", wall_s=50.0, cost_usd=0.02),
        finished(4, "t3", "pass", wall_s=10.0, cost_usd=0.03),
        finished(5, "t4", "pass", label="b", wall_s=20.0),
        event(STARTED_EVENT, 5, key="t5", task="t5", label="a", repeat=0, attempt=0),
    ]

    p = progress(events, now=T0 + timedelta(minutes=5))

    assert (p["state"], p["total"], p["done"]) == ("running", 10, 4)
    # t4 has no cost, so neither the run's total nor arm b's mean is known.
    assert (p["cost_usd"], p["cost_unknown"], p["arms"]["b"]["mean_cost_usd"]) == (None, 1, None)
    # t1 cost both its attempts: 0.005 for the invalid one and 0.01 for the pass.
    assert p["arms"]["a"]["mean_cost_usd"] == pytest.approx((0.015 + 0.03) / 2)
    assert p["verdicts"] == {"pass": 3, "fail": 1}
    assert p["pass_rate"] == 0.75
    assert p["arms"]["a"]["median_pass_s"] == 20.0
    assert p["arms"]["b"]["pass_rate"] == 0.5
    assert [t["key"] for t in p["running"]] == ["t5"]
    # Four trials in five minutes leaves six for seven and a half more.
    assert p["eta_s"] == 450


def test_a_finished_run_has_no_eta_and_stops_its_clock() -> None:
    events = [
        event(RUN_EVENT, 0, state="running", total=1, concurrency=1, labels=["a"]),
        finished(2, "t1", "pass", wall_s=5.0),
        event(RUN_EVENT, 3, state="finished"),
    ]
    p = progress(events, now=T0 + timedelta(hours=1))
    assert (p["state"], p["eta_s"], p["elapsed_s"]) == ("finished", None, 180.0)


def test_runs_lists_each_run_once_newest_first() -> None:
    old = event(RUN_EVENT, 0, state="running", total=3, labels=["a"])
    new = Event(name=RUN_EVENT, session="run-2", at=T0 + timedelta(minutes=9),
                payload={"state": "running", "total": 4, "labels": ["b"]})  # fmt: skip
    resumed = event(RUN_EVENT, 5, state="running", total=3, labels=["a"])
    assert [r["run_id"] for r in runs([old, new, resumed])] == ["run-2", "run-1"]


def test_a_feed_that_cannot_send_goes_quiet_after_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeClient(fail=True)
    feed = LiveFeed(client, "run-1")  # ty: ignore[invalid-argument-type]
    feed.run_started(total=1, already_done=0, concurrency=1, labels=["a"], kept=[], planned=["k"])
    feed.run_finished()
    assert sum("live feed stopped" in r.message for r in caplog.records) == 1


def test_the_feed_is_off_without_langfuse_or_when_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "LANGFUSE_PUBLIC_KEY" not in os.environ
    assert LiveFeed.open("run-1") is None
    monkeypatch.setenv(live.OFF_ENV, "0")
    assert LiveFeed.open("run-1") is None


async def test_the_runner_sends_each_trial_and_writes_nothing_into_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    monkeypatch.setattr(runner.LiveFeed, "open", classmethod(lambda cls, run_id: cls(client, run_id)))
    out = tmp_path / "r"

    await run_async(
        discover([TASKS]), ["null-claim"], [None], RunOptions(out=out, isolation=Isolation.HOST, quiet=True)
    )

    results = read_results(out / "results.jsonl")
    names = [name for name, _ in client.sent]
    assert names[0] == RUN_EVENT and client.sent[0][1]["total"] == len(results)
    assert names[-1] == RUN_EVENT and client.sent[-1][1]["state"] == "finished"
    assert names.count(STARTED_EVENT) == names.count(FINISHED_EVENT) == len(results)
    sent = {p["key"]: p["verdict"] for name, p in client.sent if name == FINISHED_EVENT}
    assert sent == {r.key: r.verdict.value for r in results}
    assert all(Verdict(v) for v in sent.values())
    assert client.flushed >= 1
    assert not any("live" in p.name for p in out.rglob("*"))


def test_a_v2_row_reads_back_with_its_payload_parsed_or_as_a_string() -> None:
    payload = {"at": "2026-11-16T16:53:00+00:00", "key": "k0", "verdict": "pass"}
    row = {"name": FINISHED_EVENT, "sessionId": "run-1", "startTime": "2026-11-16T16:53:00.975Z"}
    for metadata in ({live.PAYLOAD: payload}, {live.PAYLOAD: json.dumps(payload)}):
        read = live._event({**row, "metadata": metadata})
        assert read is not None and read.payload == payload and read.session == "run-1"
    assert live._event({**row, "metadata": {}}) is None


def test_a_resumed_run_is_running_again_despite_its_earlier_end() -> None:
    events = [
        event(RUN_EVENT, 0, state="running", total=4, already_done=0, concurrency=1, labels=["a"]),
        finished(1, "t1", "pass", wall_s=5.0, cost_usd=0.01),
        event(RUN_EVENT, 2, state="finished"),
        event(
            RUN_EVENT,
            10,
            state="running",
            total=4,
            already_done=1,
            concurrency=1,
            labels=["a"],
            kept=["t1"],
        ),
        finished(12, "t2", "pass", wall_s=5.0, cost_usd=0.01),
    ]
    p = progress(events, now=T0 + timedelta(minutes=14))
    assert (p["state"], p["elapsed_s"], p["cost_usd"]) == ("running", 240.0, 0.02)
    # One trial in the four minutes since the resume leaves two for eight more.
    assert p["eta_s"] == 480


async def test_a_failure_reason_reaches_the_feed_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-live-" + "x" * 32
    monkeypatch.setenv("PROVIDER_API_KEY", secret)
    client = FakeClient()
    monkeypatch.setattr(runner.LiveFeed, "open", classmethod(lambda cls, run_id: cls(client, run_id)))
    real_trial = runner.Session.trial

    async def leaking(self: runner.Session, plan: runner.TrialPlan) -> runner.TrialRun:
        run = await real_trial(self, plan)
        leaked = run.result.model_copy(update={"verdict": Verdict.INVALID, "invalid_reason": f"boom {secret}"})
        return runner.TrialRun(leaked, run.evidence)

    monkeypatch.setattr(runner.Session, "trial", leaking)
    await run_async(
        discover([TASKS]), ["null-claim"], [None], RunOptions(out=tmp_path / "r", isolation=Isolation.HOST, quiet=True)
    )

    reasons = [p["reason"] for name, p in client.sent if name == FINISHED_EVENT]
    assert reasons and all(secret not in r and r.startswith("boom ") for r in reasons)


def test_a_retry_in_flight_replaces_its_earlier_attempt() -> None:
    events = [
        event(RUN_EVENT, 0, state="running", total=1, already_done=0, concurrency=1, labels=["a"]),
        event(STARTED_EVENT, 0, key="t1", task="t1", label="a", repeat=0, attempt=0),
        finished(1, "t1", "invalid", reason="site down"),
        event(STARTED_EVENT, 2, key="t1", task="t1", label="a", repeat=0, attempt=1),
    ]
    p = progress(events, now=T0 + timedelta(minutes=3))
    assert (p["done"], p["verdicts"], [t["attempt"] for t in p["running"]]) == (0, {}, [1])


def test_a_resume_that_kept_nothing_drops_the_earlier_trials() -> None:
    events = [
        event(RUN_EVENT, 0, state="running", total=2, already_done=0, concurrency=1, labels=["a"]),
        finished(1, "old1", "fail", cost_usd=0.5),
        event(RUN_EVENT, 2, state="finished"),
        event(RUN_EVENT, 10, state="running", total=2, already_done=0, concurrency=1, labels=["a"]),
        finished(11, "new1", "pass", cost_usd=0.01),
    ]
    p = progress(events, now=T0 + timedelta(minutes=12))
    assert (p["done"], p["verdicts"], p["cost_usd"]) == (1, {"pass": 1}, 0.01)


def test_trials_done_before_the_feed_saw_them_count_toward_progress() -> None:
    events = [
        event(RUN_EVENT, 0, state="running", total=10, already_done=6, concurrency=1, labels=["a"]),
        finished(2, "t7", "pass", cost_usd=0.01),
    ]
    p = progress(events, now=T0 + timedelta(minutes=2))
    # Seven of ten done, one in two minutes: three left take six.
    assert (p["done"], p["eta_s"]) == (7, 360)
    # Six of the seven have no verdict or cost here, so neither the pass rate nor the cost is the run's.
    assert (p["unseen"], p["pass_rate"], p["cost_usd"], p["arms"]["a"]["pass_rate"]) == (6, None, None, None)


def test_watch_without_langfuse_is_an_environment_error() -> None:
    from parallax.track.sink import TrackError

    with pytest.raises(TrackError, match="LANGFUSE_PUBLIC_KEY"):
        live.LiveSource.from_env()


def test_a_finished_run_has_nothing_running() -> None:
    events = [
        event(RUN_EVENT, 0, state="running", total=2, already_done=0, concurrency=1, labels=["a"]),
        event(STARTED_EVENT, 0, key="t1", task="t1", label="a", repeat=0, attempt=0),
        event(RUN_EVENT, 1, state="finished"),
    ]
    assert progress(events, now=T0 + timedelta(minutes=5))["running"] == []


def test_a_resumed_retry_of_an_earlier_invalid_trial_counts_as_done() -> None:
    events = [
        event(RUN_EVENT, 0, state="running", total=3, already_done=0, concurrency=1, labels=["a"]),
        finished(1, "t1", "invalid", reason="site down"),
        event(RUN_EVENT, 2, state="finished"),
        # t2 and t3 passed while the feed was down; t1 is re-run.
        event(RUN_EVENT, 10, state="running", total=3, already_done=2, concurrency=1, labels=["a"], kept=["t2", "t3"]),
        finished(11, "t1", "pass", attempt=0, cost_usd=0.01),
        event(RUN_EVENT, 12, state="finished"),
    ]
    assert progress(events, now=T0 + timedelta(minutes=13))["done"] == 3


def test_a_result_waiting_for_its_retry_is_not_done() -> None:
    events = [
        event(RUN_EVENT, 0, state="running", total=1, already_done=0, concurrency=1, labels=["a"]),
        event(STARTED_EVENT, 0, key="t1", task="t1", label="a", repeat=0, attempt=0),
        finished(1, "t1", "invalid", reason="site down", retry_pending=True),
    ]
    p = progress(events, now=T0 + timedelta(minutes=2))
    assert (p["done"], [(t["key"], t["waiting"], t["attempt"]) for t in p["running"]]) == (0, [("t1", True, 1)])


def test_watch_refuses_keys_of_another_project(monkeypatch: pytest.MonkeyPatch) -> None:
    from parallax.track import sink
    from parallax.track.sink import TrackError

    for name, value in {"LANGFUSE_BASE_URL": "https://lf.test", "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk",
                        sink.PROJECT_ENV: "evals"}.items():  # fmt: skip
        monkeypatch.setenv(name, value)

    def wrong(*, write: bool) -> None:
        raise TrackError("keys belong to project 'other', not 'evals'")

    monkeypatch.setattr(sink, "connect", wrong)
    with pytest.raises(TrackError, match="other"):
        live.LiveSource.from_env()


def test_a_queued_rerun_of_an_earlier_invalid_trial_is_not_done() -> None:
    events = [
        event(
            RUN_EVENT,
            0,
            state="running",
            total=3,
            already_done=0,
            concurrency=1,
            labels=["a"],
            kept=[],
        ),
        finished(1, "t1", "pass", cost_usd=0.01),
        finished(2, "t2", "invalid", reason="site down"),
        finished(3, "t3", "invalid", reason="site down"),
        event(RUN_EVENT, 4, state="finished"),
        # The resume keeps t1 and re-runs t2 and t3 one at a time; t3 has not started yet.
        event(RUN_EVENT, 10, state="running", total=3, already_done=1, concurrency=1, labels=["a"], kept=["t1"]),
        event(STARTED_EVENT, 10, key="t2", task="t2", label="a", repeat=0, attempt=1),
    ]
    p = progress(events, now=T0 + timedelta(minutes=11))
    assert (p["done"], p["verdicts"], p["cost_usd"], p["unseen"]) == (1, {"pass": 1}, 0.01, 0)


async def test_a_resumed_retry_is_pending_while_this_run_has_retries_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    monkeypatch.setattr(runner.LiveFeed, "open", classmethod(lambda cls, run_id: cls(client, run_id)))
    real_trial = runner.Session.trial

    async def flaky(self: runner.Session, plan: runner.TrialPlan) -> runner.TrialRun:
        run = await real_trial(self, plan)
        if plan.attempt < 3:  # an earlier invocation already used attempts 0 to 2
            run = runner.TrialRun(run.result.model_copy(update={"verdict": Verdict.INVALID, "attempt": plan.attempt,
                                  "invalid_reason": "environment interruption: site down"}), run.evidence)  # fmt: skip
        return run

    monkeypatch.setattr(runner.Session, "trial", flaky)
    monkeypatch.setattr(runner, "transient_invalid", lambda r: r.verdict is Verdict.INVALID)
    real_execute = runner.execute_all

    async def from_attempt_two(session, todo, opts, *, retries_left=0):  # noqa: ANN001, ANN202
        todo = [replace(p, attempt=max(p.attempt, 2)) for p in todo]
        return await real_execute(session, todo, opts, retries_left=retries_left)

    monkeypatch.setattr(runner, "execute_all", from_attempt_two)
    await run_async(
        discover([TASKS]), ["null-claim"], [None],
        RunOptions(out=tmp_path / "r", isolation=Isolation.HOST, quiet=True, retry_invalid=1, retry_wait_s=0),
    )  # fmt: skip
    first = [p for name, p in client.sent if name == FINISHED_EVENT and p["attempt"] == 2]
    assert first and all(p["retry_pending"] for p in first)
    assert all(not p["retry_pending"] for name, p in client.sent if name == FINISHED_EVENT and p["attempt"] == 3)


def test_events_keep_subsecond_order() -> None:
    assert len(live._now().split(".")[-1].split("+")[0]) == 6


def test_watch_on_a_taken_port_is_an_environment_error() -> None:
    import socket

    from parallax.track import watch
    from parallax.track.sink import TrackError

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(TrackError, match="cannot serve"):
            watch.serve(live.LiveSource("https://lf.test", "pk", "sk"), host="127.0.0.1", port=taken.getsockname()[1])


def test_a_trial_dropped_from_the_plan_is_not_kept() -> None:
    events = [
        event(RUN_EVENT, 0, state="running", total=3, already_done=0, concurrency=1, labels=["a"], kept=[]),
        finished(1, "t1", "pass", cost_usd=0.01),
        finished(2, "gone", "fail", cost_usd=0.5),
        event(RUN_EVENT, 3, state="finished"),
        # t2 passed while the feed was down; the resume dropped "gone" from the plan and keeps t1 and t2.
        event(RUN_EVENT, 10, state="running", total=3, already_done=2, concurrency=1, labels=["a"], kept=["t1", "t2"]),
    ]
    p = progress(events, now=T0 + timedelta(minutes=11))
    assert (p["done"], p["unseen"], p["verdicts"], p["pass_rate"]) == (2, 1, {"pass": 1}, None)


def test_a_retried_attempt_still_counts_toward_the_cost() -> None:
    events = [
        event(RUN_EVENT, 0, state="running", total=1, already_done=0, concurrency=1, labels=["a"], kept=[]),
        finished(1, "t1", "invalid", reason="site down", cost_usd=0.2, retry_pending=True),
        event(STARTED_EVENT, 2, key="t1", task="t1", label="a", repeat=0, attempt=1),
        finished(3, "t1", "pass", attempt=1, cost_usd=0.01),
    ]
    p = progress(events, now=T0 + timedelta(minutes=4))
    assert p["cost_usd"] == pytest.approx(0.21) and p["arms"]["a"]["mean_cost_usd"] == pytest.approx(0.21)


def test_a_rerun_trial_keeps_the_cost_of_its_attempts_before_the_resume() -> None:
    events = [
        event(
            RUN_EVENT, 0, state="running", total=1, already_done=0, concurrency=1, labels=["a"], kept=[], planned=["t1"]
        ),
        finished(1, "t1", "invalid", reason="site down", cost_usd=0.2),
        event(RUN_EVENT, 2, state="finished"),
        event(
            RUN_EVENT,
            10,
            state="running",
            total=1,
            already_done=0,
            concurrency=1,
            labels=["a"],
            kept=[],
            planned=["t1"],
        ),
        finished(11, "t1", "pass", attempt=1, cost_usd=0.01),
    ]
    p = progress(events, now=T0 + timedelta(minutes=12))
    assert (p["done"], p["cost_usd"]) == (1, pytest.approx(0.21))


async def test_runs_sharing_a_directory_name_get_their_own_live_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions: list[str] = []

    def opened(cls: type[LiveFeed], session: str) -> LiveFeed:
        sessions.append(session)
        return cls(FakeClient(), session)

    monkeypatch.setattr(runner.LiveFeed, "open", classmethod(opened))
    for parent in ("a", "b"):
        await run_async(discover([TASKS]), ["null-claim"], [None],
                        RunOptions(out=tmp_path / parent / "r", isolation=Isolation.HOST, quiet=True))  # fmt: skip

    assert len(set(sessions)) == 2
    assert all(s.startswith("r-") for s in sessions)
    for parent, session in zip(("a", "b"), sessions, strict=True):
        manifest = RunManifest.model_validate_json((tmp_path / parent / "r" / "manifest.json").read_text())
        assert (manifest.run_id, manifest.live_session) == ("r", session)


async def test_a_resumed_run_keeps_its_live_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions: list[str] = []

    def opened(cls: type[LiveFeed], session: str) -> LiveFeed:
        sessions.append(session)
        return cls(FakeClient(), session)

    monkeypatch.setattr(runner.LiveFeed, "open", classmethod(opened))
    opts = RunOptions(out=tmp_path / "r", isolation=Isolation.HOST, quiet=True)
    await run_async(discover([TASKS]), ["null-claim"], [None], opts)
    await run_async(discover([TASKS]), ["null-claim"], [None], opts)

    assert len(sessions) == 2 and sessions[0] == sessions[1]


def test_a_manifest_written_before_live_sessions_still_loads(tmp_path: Path) -> None:
    manifest = RunManifest(run_id="r", created_at=T0, parallax_version="0", harnesses=(), models=(), tasks=(),
                           repeats=1, judge=(), isolation=Isolation.HOST, selection="", grading_policy="")  # fmt: skip
    old = manifest.model_dump(mode="json", exclude={"live_session"})
    assert RunManifest.model_validate(old).live_session is None
