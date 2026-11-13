"""The seams a world-less browser task runs through: task sources, the trial hooks, attempts, the browser judge, its
identity and the refusal to pool across identities. Offline: every judge request answers from a MockTransport."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from parallax import browser_evidence, extensions, jev, runner, semantic
from parallax.browser_evidence import BrowserEvidence, BrowserPage, BrowserStep, parts
from parallax.judge import JudgeConfig, JudgePanel
from parallax.judging import Decision, JudgePolicy, combined, identity
from parallax.models import (
    Area,
    Capability,
    CostSource,
    HarnessInfo,
    HarnessKind,
    Isolation,
    JsonObject,
    TaskRecord,
    Timing,
    Trial,
    TrialResult,
    TurnInput,
    TurnOutput,
    TurnStatus,
    Verdict,
)
from parallax.protocols import BoxHandle
from parallax.runner import RunOptions, decide, read_results, run_async
from parallax.stats import MixedJudgeIdentities, judge_identity
from parallax.task import TaskError, discover, record_hash, select, task_from_record
from parallax.track.rows import load_run, row_scores

TASKS = Path(__file__).parent / "fixtures" / "tasks"
RECORD = TaskRecord(
    id="browser-find-price",
    version=1,
    area=Area.BROWSER,
    summary="Find a listed price",
    prompt="Find the price of the blue kettle on the shop's site.",
    grading="browser-evidence-v1",
    data={"start_url": "https://shop.example/", "intended": "$24.00"},
)
JEV = jev.JevConfig(base_url="https://fake/v1", model="jev", api_key="test")
INFO = HarnessInfo(
    name="browsing", kind=HarnessKind.PRODUCT, capabilities=frozenset(), pins_model=False,
    cost_source=CostSource.HARNESS, isolation=Isolation.HOST,
)  # fmt: skip
OBSERVED = BrowserEvidence(
    final_page=BrowserPage(url="https://shop.example/kettle", title="Blue kettle", text="Blue kettle $24.00"),
    answer="$24.00",
    steps=(BrowserStep(url="https://shop.example/", action="click Kettles", page=0),),
    pages=(BrowserPage(url="https://shop.example/", title="Shop", text="Kettles Toasters"),),
)


def png(side: int = 64) -> str:
    """A valid grey PNG, base64 without a data-URL prefix, as a harness reports a screenshot."""
    raw = b"".join(b"\x00" + b"\x80" * side for _ in range(side))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return base64.b64encode(data).decode()


class Browsing:
    """A world-less harness that reports browser evidence and every optional trial hook."""

    info = INFO

    def __init__(self, outputs: list[TurnOutput] | None = None) -> None:
        self.outputs = outputs or [TurnOutput(status=TurnStatus.COMPLETED, answer="$24.00", session_id=None)]
        self.trials: list[Trial] = []
        self.turns: list[TurnInput] = []
        self.trial_metadata: JsonObject = {"browser": OBSERVED.model_dump(mode="json")}

    async def start(self, trial: Trial, box: BoxHandle) -> None:
        self.trials.append(trial)

    async def turn(self, t: TurnInput) -> TurnOutput:
        self.turns.append(t)
        return self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]

    async def stop(self) -> None:
        return None

    async def version(self, box: BoxHandle) -> str:
        return "1"

    def trial_timing(self) -> Timing:
        return Timing(basis="agent-run", breakdown={"browser_start": 800.0, "agent": 4000.0})

    def trial_costs(self) -> dict[str, float | None]:
        return {"agent": 0.25, "browser": 0.05}

    def trial_details(self) -> JsonObject:
        return {"lease_wait_ms": 120}


def source_task():
    return task_from_record("shop", RECORD)


async def run_browsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: Browsing,
    task=None,
    **opts: object,
) -> Path:
    monkeypatch.setattr(runner, "load_harness", lambda name: harness)
    options = RunOptions(out=tmp_path / "run", isolation=Isolation.HOST, quiet=True, **opts)  # type: ignore[arg-type]
    return await run_async([source_task() if task is None else task], ["browsing"], [None], options)


# ---------------------------------------------------------------------------
# task sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "same"),
    [
        ({}, True),
        ({"data": {"intended": "$24.00", "start_url": "https://shop.example/"}}, True),  # key order is not content
        ({"prompt": "Find the price of the red kettle."}, False),
        ({"data": {"start_url": "https://shop.example/", "intended": "$25.00"}}, False),
        ({"grading": "another-policy"}, False),
    ],
)
def test_a_task_records_identity_is_a_hash_of_its_declared_content(change: dict[str, object], same: bool) -> None:
    assert (record_hash(RECORD.model_copy(update=change)) == record_hash(RECORD)) is same


def test_a_source_task_has_no_world_one_turn_and_its_policys_checks() -> None:
    task = source_task()
    assert task.seed is None and task.oracle is None and task.source == "shop"
    assert [t.text for t in task.turns] == [RECORD.prompt]
    assert [c.id for c in task.checks.checks] == ["evidence_available", "task_satisfied"]
    assert task.hash == record_hash(RECORD)
    assert select([*discover([TASKS]), task], "shop", suites={}) == [task]  # the source name selects its tasks
    with pytest.raises(TaskError, match="no-such-policy"):
        task_from_record("shop", RECORD.model_copy(update={"grading": "no-such-policy"}))
    with pytest.raises(TaskError, match="more than 366 days ahead"):  # an unbounded offset would overflow mid-run
        task_from_record("shop", RECORD.model_copy(update={"prompt": "Book a flight on {today+999999999}."}))


def test_answer_schema_reaches_the_browser_judge_state() -> None:
    schema = {"type": "object", "properties": {"price": {"type": "string"}}}
    task = source_task().model_copy(update={"answer_schema": schema})
    state = browser_evidence.judge_state(task, OBSERVED, ("$24.00",), {})
    assert state["answer_schema"] == schema


# ---------------------------------------------------------------------------
# a world-less trial and the hooks it reports
# ---------------------------------------------------------------------------


async def test_a_world_less_trial_mounts_no_world_and_its_hooks_reach_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Browsing()
    run_dir = await run_browsing(tmp_path, monkeypatch, harness)
    (trial,) = harness.trials
    assert trial.mcp is None and trial.user is None
    (row,) = read_results(run_dir / "results.jsonl")
    assert (row.timing_basis, row.timing_breakdown) == ("agent-run", {"browser_start": 800.0, "agent": 4000.0})
    assert row.cost_by_component == {"agent": 0.25, "browser": 0.05}
    assert row.cost_usd == pytest.approx(0.30)
    assert row.details == {"lease_wait_ms": 120}
    assert row.judge_identity is not None and row.judge_identity.startswith("browser-evidence-v1:")
    # No judge is configured offline, so the policy abstains: unjudged, never a pass or a fail.
    assert row.verdict is Verdict.INVALID
    assert row.dir is not None and not (run_dir / row.dir / "world_evidence.json").exists()


async def test_today_is_substituted_at_start_but_the_template_hash_and_details_are_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = RECORD.model_copy(update={"prompt": "Today is {today}. Find the price of a flight on {today+28}."})
    task = task_from_record("shop", record)
    harness = Browsing()
    run_dir = await run_browsing(tmp_path, monkeypatch, harness, task=task)
    [turn] = harness.turns
    [row] = read_results(run_dir / "results.jsonl")
    today = datetime.now(UTC).date()
    assert "{today}" in task.turns[0].text
    assert (
        turn.text
        == f"Today is {today.isoformat()}. Find the price of a flight on {(today + timedelta(days=28)).isoformat()}."
    )
    assert row.task_hash == record_hash(record)
    assert row.details["today"] == today.isoformat()


async def test_missing_task_secret_is_invalid_before_the_harness_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from parallax.harnesses.browser.harnesses import BrowserHarnessCloud

    monkeypatch.delenv("PARALLAX_TEST_SHOP_PASSWORD", raising=False)
    record = RECORD.model_copy(
        update={
            "requires": frozenset({Capability.BROWSER, Capability.CREDENTIALS}),
            "secrets": {"shop_password": "PARALLAX_TEST_SHOP_PASSWORD"},
        }
    )
    task = task_from_record("shop", record)
    harness_plan = runner.HarnessPlan(
        name="browser-use-cloud", info=BrowserHarnessCloud.info, version="1", config_hash="c"
    )
    (plan,) = runner.plan_trials([task], [harness_plan], [None], RunOptions(out=tmp_path, isolation=Isolation.HOST))
    session = runner.Session(SimpleNamespace(isolation=Isolation.HOST), tmp_path, None, None)
    result = await session.trial(plan)
    assert result.result.verdict is Verdict.INVALID
    assert result.result.invalid_reason is not None
    assert (
        "missing secret environment variable PARALLAX_TEST_SHOP_PASSWORD for shop_password"
        in result.result.invalid_reason
    )


@pytest.mark.parametrize(
    ("costs", "total", "kept"),
    [
        ({"agent": 0.1, "browser": 0.02}, 0.12, {"agent": 0.1, "browser": 0.02}),
        ({"agent": 0.1, "browser": None}, None, {"agent": 0.1}),  # one unknown component: the total is unknown
    ],
)
def test_reported_cost_components_are_the_trials_cost(
    tmp_path: Path, costs: dict[str, float | None], total: float | None, kept: dict[str, float]
) -> None:
    (plan,) = runner.plan_trials(
        [source_task()], [runner.HarnessPlan(name="h", info=INFO, version="1", config_hash="c")], [None],
        RunOptions(out=tmp_path, isolation=Isolation.HOST),
    )  # fmt: skip
    row = runner.result_row(
        plan, verdict=Verdict.FAIL, invalid_reason=None, na_reason=None, status=None, checks=(), usage=(),
        cost_source=CostSource.HARNESS, actual=(), unobserved=False, timings=(None, None, None, None), judge=(),
        same_family=False, web=runner.WebIsolation.NONE, rel_dir=None,
        reported=runner.Transcript(answers=[], costs=costs),
    )  # fmt: skip
    assert row.cost_usd == (pytest.approx(total) if total is not None else None)
    assert row.cost_by_component == kept


# ---------------------------------------------------------------------------
# attempts and environment interruptions
# ---------------------------------------------------------------------------

INTERRUPTED = TurnOutput(
    status=TurnStatus.HARNESS_ERROR, answer="", session_id=None, error="environment interruption: shop returned 503"
)


async def test_every_attempt_keeps_its_row_and_directory_and_an_interruption_waits_before_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []
    sleep = asyncio.sleep

    async def recorded(delay: float, *args: object) -> None:
        if delay >= 5:  # the retry waits; the embedded servers' own short polls run as they are
            waits.append(delay)
            delay = 0
        await sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", recorded)
    harness = Browsing([INTERRUPTED, INTERRUPTED, TurnOutput(status=TurnStatus.COMPLETED, answer="", session_id=None)])
    run_dir = await run_browsing(tmp_path, monkeypatch, harness, retry_invalid=2, retry_wait_s=5.0)

    rows = read_results(run_dir / "results.jsonl")
    assert [r.attempt for r in rows] == [0, 1, 2]
    assert rows[0].invalid_reason == "environment interruption: shop returned 503"
    assert waits == [5.0, 10.0]  # doubled each round
    dirs = [r.dir for r in rows]
    assert len(set(dirs)) == 3 and all(d is not None and (run_dir / d / "result.json").is_file() for d in dirs)
    assert not (run_dir / str(dirs[0]) / "judgments.jsonl").exists()  # an interrupted attempt is never judged
    loaded = load_run(run_dir)
    assert (len(loaded.attempts), len(loaded.results), loaded.retries) == (3, 1, 2)
    assert [r.attempt for r in loaded.physical_runs(rows[0].key)] == [0, 1, 2]
    assert loaded.results[0].attempt == 2


async def test_a_resumed_invalid_trial_takes_the_next_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await run_browsing(tmp_path, monkeypatch, Browsing([INTERRUPTED]), retry_invalid=0)
    run_dir = await run_browsing(tmp_path, monkeypatch, Browsing([INTERRUPTED]), retry_invalid=0)
    assert [(r.attempt, r.dir) for r in read_results(run_dir / "results.jsonl")] == [
        (0, "trials/browser-find-price/browsing/-/0"),
        (1, "trials/browser-find-price/browsing/-/0-attempt1"),
    ]


async def test_a_trial_the_budget_cannot_hold_is_refused_and_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = await run_browsing(
        tmp_path, monkeypatch, Browsing(), repeats=2, concurrency=1, budget_usd=0.5, reserve_usd=0.4, retry_invalid=1
    )
    rows = sorted(read_results(run_dir / "results.jsonl"), key=lambda r: r.repeat)
    assert [r.invalid_reason is not None and "budget refused" in r.invalid_reason for r in rows] == [False, True]
    events = [json.loads(line) for line in (run_dir / "budget.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["header", "reserve", "settle"]
    assert events[2]["charge_usd"] == pytest.approx(0.30)  # settled from the row's cost


async def test_a_resume_reopens_the_existing_budget_without_cli_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = await run_browsing(
        tmp_path, monkeypatch, Browsing(), repeats=2, concurrency=1, budget_usd=0.5, reserve_usd=0.4, retry_invalid=0
    )
    journal = run_dir / "budget.jsonl"
    before = journal.read_text()

    await run_browsing(tmp_path, monkeypatch, Browsing(), repeats=2, concurrency=1, retry_invalid=0)

    rows = sorted(read_results(run_dir / "results.jsonl"), key=lambda r: (r.repeat, r.attempt))
    assert rows[-1].repeat == 1
    assert rows[-1].invalid_reason is not None and "budget refused" in rows[-1].invalid_reason
    assert journal.read_text() == before


# ---------------------------------------------------------------------------
# the browser judge
# ---------------------------------------------------------------------------


def jev_answering(probabilities: dict[str, float], seen: list[JsonObject]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        answers = {k: {"type": "noul", "noul": probabilities[k]} for k in body["questions"]}
        return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 1, "output_tokens": 0}})

    return httpx.MockTransport(handle)


async def grade(product: JsonObject | None, transport: httpx.MockTransport, **route: object) -> semantic.Graded:
    async with httpx.AsyncClient(transport=transport) as client:
        return await semantic.grade_checks(
            source_task(), None, route.get("panel"), route.get("config"), None, client=client,  # type: ignore[arg-type]
            jev_config=JEV, execution={"status": "completed"}, product=product, answers=("$24.00",),
        )  # fmt: skip


@pytest.mark.parametrize(
    ("available", "satisfied", "verdict"),
    [
        (0.99, 0.99, Verdict.PASS),
        (0.99, 0.01, Verdict.FAIL),
        (0.01, 0.99, Verdict.INVALID),  # evidence the judge finds unavailable measured no agent
        (0.5, 0.99, Verdict.INVALID),  # unsettled and no fallback: unjudged
    ],
)
async def test_the_browser_policy_answers_both_questions(available: float, satisfied: float, verdict: Verdict) -> None:
    seen: list[JsonObject] = []
    probabilities = {"evidence_available": available, "task_satisfied": satisfied}
    graded = await grade({"browser": OBSERVED.model_dump(mode="json")}, jev_answering(probabilities, seen))
    assert decide(None, [], runner.TrajectoryStatus.COMPLETED, graded.checks)[0] is verdict
    (body,) = seen
    state = body["state"]
    assert state["data"] == RECORD.data and state["answer"] == "$24.00"
    assert [p["id"] for p in state["pages"]] == ["page-1", "page-2"] and state["timeline"][0]["saw"] == "page-1"
    assert graded.judge_identity == identity(browser_evidence.JUDGE, None, None, JEV)


async def test_a_page_read_again_as_the_final_page_is_listed_once() -> None:
    seen: list[JsonObject] = []
    revisit = OBSERVED.model_copy(update={"final_page": OBSERVED.pages[0]})
    await grade(
        {"browser": revisit.model_dump(mode="json")},
        jev_answering({"evidence_available": 0.9, "task_satisfied": 0.9}, seen),
    )
    (body,) = seen
    assert [(p["id"], p.get("final")) for p in body["state"]["pages"]] == [("page-1", True)]


@pytest.mark.parametrize(
    ("product", "detail"),
    [
        (None, "reported no browser evidence"),
        ({"browser": {"final_page": None, "answer": "$24.00"}}, "no record of a page or a step"),
        ({"browser": {"final_page": None}}, "malformed"),
        (
            {"browser": {**OBSERVED.model_dump(mode="json"), "steps": [{"url": "u", "action": "a", "page": 7}]}},
            "page 7",
        ),
    ],
)
async def test_missing_browser_evidence_is_unjudged_without_a_request(product: JsonObject | None, detail: str) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("evidence that cannot be judged must not be sent")

    graded = await grade(product, httpx.MockTransport(unexpected))
    assert all(c.passed is None and detail in c.detail for c in graded.checks)


async def test_a_screenshot_reaches_the_fallback_as_an_image() -> None:
    seen: list[JsonObject] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        items = [{"q": q, "why": "seen", "yes": True} for q in ("evidence_available", "task_satisfied")]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"items": items})}}]})

    shot = OBSERVED.model_copy(
        update={"final_page": OBSERVED.final_page.model_copy(update={"screenshot_png_b64": png()})}
    )  # type: ignore[union-attr]
    graded = await grade(
        {"browser": shot.model_dump(mode="json")}, httpx.MockTransport(handle),
        panel=JudgePanel(models=("anthropic/claude-opus-5.5",)), config=JudgeConfig(base_url="https://fake/v1", api_key="t"),
    )  # fmt: skip
    (body,) = seen  # Jev reads text only, so a state holding an image goes to the fallback alone
    content = body["messages"][1]["content"]
    assert [p["image_url"]["url"] for p in content if p["type"] == "image_url"] == ["data:image/png;base64," + png()]
    assert png() not in content[0]["text"]  # the image travels once, as an image, never also as text
    assert all(c.passed is True for c in graded.checks)


PAGE_TEXT = 'Blue kettle, 1.7 litres, émaillé "steel".\n' * 120
CONTROLS = [{"field": f"option-{i}", "value": "x" * 40} for i in range(60)]


@pytest.mark.parametrize(
    "pages",
    [
        [{"id": "page-1", "url": "u1", "title": "t", "text": PAGE_TEXT}],
        [{"id": "page-1", "url": "u1", "title": "t", "text": PAGE_TEXT, "controls": CONTROLS}],
        [
            {"id": "page-1", "url": "u1", "title": "t", "text": "short"},
            {
                "id": "page-2",
                "url": "u2",
                "title": "t",
                "text": PAGE_TEXT,
                "screenshot": "data:image/png;base64," + png(),
            },
            {"id": "page-3", "url": "u3", "title": "t", "text": "", "controls": CONTROLS},
        ],
    ],
)
def test_a_split_state_sends_every_byte_of_every_page_once_within_the_limit(pages: list[JsonObject]) -> None:
    limit = 1500
    state: JsonObject = {"task": {"id": "t"}, "answer": "$24.00", "pages": pages}
    divided = parts(state, limit)
    assert len(divided) > 1
    assert all(browser_evidence._size(part) <= limit for part in divided)  # noqa: SLF001
    assert all(
        {k: v for k, v in part.items() if k != "pages"} == {"task": {"id": "t"}, "answer": "$24.00"} for part in divided
    )
    pieces = [piece for part in divided for piece in part["pages"]]  # type: ignore[union-attr]
    for page in pages:
        mine = [p for p in pieces if p["id"] == page["id"]]  # type: ignore[index]
        assert "".join(str(p.get("text", "")) for p in mine) == page["text"]  # type: ignore[union-attr]
        assert [c for p in mine for c in p.get("controls", [])] == page.get("controls", [])  # type: ignore[union-attr]
        assert [p["screenshot"] for p in mine if "screenshot" in p] == (
            [page["screenshot"]] if "screenshot" in page else []
        )  # type: ignore[index,operator]


@pytest.mark.parametrize(
    ("answers", "want"),
    [((False, True), False), ((True, None), True), ((None, True, False), False), ((None, None), None)],
)
def test_part_decisions_combine_with_a_false_standing(answers: tuple[bool | None, ...], want: bool | None) -> None:
    assert combined([Decision(passed=a, source="jev", reason=str(a)) for a in answers]).passed is want


# ---------------------------------------------------------------------------
# judging identity
# ---------------------------------------------------------------------------

POLICY = JudgePolicy(name="p-v1", questions={"q": "Is it done?"}, evidence_form="f-v1", part_bytes=1000)
PANEL, CONFIG = JudgePanel(models=("m/a",)), JudgeConfig(base_url="https://judge/v1", api_key="k")


@pytest.mark.parametrize(
    ("change", "same"),
    [
        (lambda: (POLICY, PANEL, CONFIG.model_copy(update={"api_key": "other"}), JEV), True),  # a key is no identity
        (lambda: (POLICY.model_copy(update={"questions": {"q": "Is it finished?"}}), PANEL, CONFIG, JEV), False),
        (lambda: (POLICY.model_copy(update={"evidence_form": "f-v2"}), PANEL, CONFIG, JEV), False),
        (lambda: (POLICY.model_copy(update={"part_bytes": 2000}), PANEL, CONFIG, JEV), False),
        (lambda: (POLICY.model_copy(update={"use_jev": False}), PANEL, CONFIG, JEV), False),
        (lambda: (POLICY, JudgePanel(models=("m/b",)), CONFIG, JEV), False),
        (lambda: (POLICY, PANEL, CONFIG.model_copy(update={"base_url": "https://other/v1"}), JEV), False),
        (lambda: (POLICY, PANEL, CONFIG, JEV.model_copy(update={"model": "jev-2"})), False),
        (lambda: (POLICY, None, None, JEV), False),
    ],
)
def test_a_judging_identity_moves_with_everything_its_judge_uses(
    change: Callable[[], tuple[JudgePolicy, JudgePanel | None, JudgeConfig | None, jev.JevConfig]], same: bool
) -> None:
    assert (identity(*change()) == identity(POLICY, PANEL, CONFIG, JEV)) is same


def test_a_run_of_source_tasks_adds_its_policys_identity_and_a_world_run_keeps_its_own() -> None:
    world = semantic.policy_identity()
    assert semantic.policy_identity(discover([TASKS])) == world
    sourced = semantic.policy_identity([source_task()])
    assert sourced.startswith(world + ":browser-evidence-v1:")
    assert extensions.grading_policies()["browser-evidence-v1"].judge is browser_evidence.JUDGE


def row(identity_: str | None, verdict: Verdict = Verdict.PASS) -> TrialResult:
    rows = read_results(Path(__file__).parent / "fixtures" / "runs" / "r1" / "results.jsonl")
    return rows[0].model_copy(update={"judge_identity": identity_, "verdict": verdict})


@pytest.mark.parametrize(
    ("identities", "want"),
    [(("a", "a"), "a"), ((None, None), None), (("a", "b"), MixedJudgeIdentities), (("a", None), MixedJudgeIdentities)],
)
def test_rows_graded_under_different_identities_never_pool(
    identities: tuple[str | None, ...], want: str | type[Exception] | None
) -> None:
    rows = [row(i) for i in identities]
    if isinstance(want, type):
        with pytest.raises(want, match="cannot pool"):
            judge_identity(rows)
        with pytest.raises(want):
            row_scores(rows, k=1, seed=0)
    else:
        assert judge_identity(rows) == want
        row_scores(rows, k=1, seed=0)


def test_a_row_refused_before_judging_does_not_split_the_identity() -> None:
    assert judge_identity([row("a"), row(None, Verdict.INVALID)]) == "a"


def test_a_source_filter_selects_a_source_task_by_its_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import dataclass

    from parallax import plugins
    from parallax.task import SOURCES, resolve

    @dataclass(frozen=True)
    class Source:
        name: str
        value: str = "fake.module:records"

        def load(self) -> Callable[[], list[TaskRecord]]:
            return lambda: [RECORD, RECORD.model_copy(update={"id": "browser-other"})]

    monkeypatch.setattr(plugins, "entry_points_in", lambda group: [Source("shop")] if group == SOURCES else [])
    assert [t.id for t in resolve(["source=shop", "browser-find-price"], suites={})] == ["browser-find-price"]
