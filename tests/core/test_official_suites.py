import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from parallax import official_suites as official
from parallax import runner
from parallax.extensions import GradeRequest
from parallax.judge import JudgeConfig, JudgePanel
from parallax.models import CheckResult, FailureMode, Isolation, TrajectoryStatus, Verdict
from parallax.runner import RunOptions, decide, read_results, run_async
from parallax.semantic import Graded
from parallax.task import discover, task_from_record

FIXTURES = Path(__file__).parent / "fixtures"


def _spec(source: str = "fixture-suite") -> official.SuiteSpec:
    data = b"fixture"
    return official.SuiteSpec(
        source=source,
        policy="fixture-policy-v1",
        upstream_commit="fixture-revision",
        dataset_url="https://example.invalid/dataset",
        dataset_sha256=hashlib.sha256(data).hexdigest(),
        dataset_revision="fixture-revision",
        expected_count=1,
    )


def test_fetch_dataset_verifies_and_atomically_caches_bytes(tmp_path):
    spec = _spec()
    calls = 0

    def send(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"fixture", request=request)

    transport = httpx.MockTransport(send)
    assert official.fetch_dataset(spec, cache=tmp_path, transport=transport) == b"fixture"
    assert official.fetch_dataset(spec, cache=tmp_path, transport=httpx.MockTransport(lambda _: 500)) == b"fixture"
    assert calls == 1


def test_fetch_dataset_rejects_bad_download_without_leaving_cache(tmp_path):
    spec = _spec()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"wrong", request=request))
    with pytest.raises(official.OfficialSuiteError, match="sha256 mismatch"):
        official.fetch_dataset(spec, cache=tmp_path, transport=transport)
    assert not list(tmp_path.iterdir())


def test_bu_dataset_decrypts_in_memory():
    key = base64.urlsafe_b64encode(hashlib.sha256(b"BU_Bench_V2").digest())
    encrypted = base64.b64encode(
        Fernet(key).encrypt(json.dumps({"tasks": [{"task_id": "a", "task": "do it"}]}).encode())
    )
    assert official._bu_rows(encrypted)[0]["task_id"] == "a"


def test_online_record_keeps_the_published_row_and_validates_start_site():
    row = {
        "task_id": "row-1",
        "confirmed_task": "Complete the task",
        "website": "https://example.test/start",
        "metadata": {"keep": True},
    }
    record = official._record(official.MIND2WEB_SPEC, row, 0, set())
    assert record.data["record"] == row
    assert record.data["website"] == row["website"]
    assert record.prompt == "Start at https://example.test/start. Complete the task"
    with pytest.raises(official.OfficialSuiteError, match="usable website"):
        official._record(
            official.MIND2WEB_SPEC,
            {**row, "website": "file:///tmp/no"},
            0,
            set(),
        )


@pytest.mark.asyncio
async def test_grade_sends_any_browser_evidence_to_the_upstream_judge(monkeypatch):
    row = {"task_id": "row-1", "confirmed_task": "Complete the task", "website": "https://example.test/start"}
    record = official._record(official.MIND2WEB_SPEC, row, 0, set())
    task = task_from_record(official.ONLINE_MIND2WEB, record)
    product = {
        "browser": {
            "final_page": {
                "url": "https://example.test/done",
                "title": "Done",
                "text": "Finished",
                "screenshot_png_b64": "iVBORw==",
            },
            "answer": "Finished",
            "steps": [{"url": "https://example.test/start", "action": "click"}],
            "pages": [],
        }
    }
    request = GradeRequest(
        task=task,
        evidence=None,
        answers=("Finished",),
        product=product,
        execution={},
        harness_model="agent/model",
        panel=JudgePanel(models=("configured/model",)),
        judge_config=JudgeConfig(base_url="https://judge.invalid/v1", api_key="test"),
        jev_config=None,
        client=httpx.AsyncClient(),
    )
    seen: dict[str, object] = {}

    async def bridge(_spec, _request, payload):
        seen.update(payload)
        return {"passed": True, "score": 1.0}

    monkeypatch.setattr(official, "_bridge", bridge)
    monkeypatch.setattr(
        official,
        "_config",
        lambda _request, _spec: official._Config(
            root=task.path,
            python=task.path / "python",
            base_url="https://judge.invalid/v1",
            api_key="test",
        ),
    )
    graded = await official._grade(official.MIND2WEB_SPEC, request)
    await request.client.aclose()
    assert graded.checks[0].passed is True
    assert graded.judge_identity and official.MIND2WEB_UPSTREAM_COMMIT in graded.judge_identity
    assert "configured/model" in graded.judge_identity
    assert seen["task"] == "Complete the task"
    assert seen["screenshots"] == ["iVBORw=="]
    assert seen["actions"]


@pytest.mark.asyncio
async def test_environment_interruption_remains_retryable(monkeypatch):
    row = {"task_id": "row-1", "confirmed_task": "Complete the task", "website": "https://example.test/start"}
    task = task_from_record(official.ONLINE_MIND2WEB, official._record(official.MIND2WEB_SPEC, row, 0, set()))
    request = GradeRequest(
        task=task,
        evidence=None,
        answers=(),
        product={"browser": {"final_page": None, "answer": None, "steps": [], "pages": []}},
        execution={"error": "environment interruption: site unavailable"},
        harness_model=None,
        panel=None,
        judge_config=None,
        jev_config=None,
        client=httpx.AsyncClient(),
    )
    graded = await official._grade(official.MIND2WEB_SPEC, request)
    await request.client.aclose()
    assert graded.checks[0].passed is None
    _, reason = decide(None, (), TrajectoryStatus.COMPLETED, graded.checks)
    assert reason == "environment interruption: site unavailable"


@pytest.mark.asyncio
async def test_a_published_judge_score_is_the_trial_score(tmp_path, monkeypatch):
    async def partial(*_: object, **__: object) -> Graded:
        check = CheckResult(
            id="upstream_judge", kind="rubric", mode=FailureMode.WRONG_OUTPUT, gate=True, advisory=False,
            weight=1.0, passed=False, detail="",
        )  # fmt: skip
        return Graded(checks=(check,), score=0.6)

    monkeypatch.setattr(runner, "grade_checks", partial)
    out = await run_async(
        discover([FIXTURES / "tasks"]),
        ["null-claim"],
        [None],
        RunOptions(out=tmp_path / "run", isolation=Isolation.HOST, quiet=True),
    )
    rows = read_results(out / "results.jsonl")
    assert rows and all(r.verdict is Verdict.FAIL and r.score == 0.6 for r in rows)
