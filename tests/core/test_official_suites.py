import asyncio
import base64
import hashlib
import importlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
from cryptography.fernet import Fernet
from parallax import official_bridge, runner
from parallax import official_suites as official
from parallax.extensions import GradeRequest
from parallax.judge import JudgeConfig, JudgePanel
from parallax.models import CheckResult, FailureMode, Isolation, TrajectoryStatus, Verdict
from parallax.official_bridge import failure
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
        client=httpx.AsyncClient(),
    )
    seen: dict[str, object] = {}

    async def bridge(_spec, _request, payload):
        seen.update(payload)
        return {"passed": True, "score": 1.0, "calls": [RECORDED_CALL, {**RECORDED_CALL, "cost_usd": 0.5}]}

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
    assert graded.unpriced_calls == 0
    assert [j.cost() for j in graded.judgments] == [(0.500034, 0)]  # every metered call, so the budget settles it
    assert graded.judge_identity and official.MIND2WEB_UPSTREAM_COMMIT in graded.judge_identity
    assert "configured/model" in graded.judge_identity
    assert seen["task"] == "Complete the task"
    assert seen["screenshots"] == ["iVBORw=="]
    assert seen["actions"]


# One OpenRouter chat completion, recorded 2026-11-17 from `openai/gpt-6.1-terra`.
RECORDED_COMPLETION = {
    "id": "gen-1794933477-hSiIOlRqkxpmRybbCoHT",
    "object": "chat.completion",
    "created": 1794933477,
    "model": "openai/gpt-6.1-terra",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14, "cost": 3.4e-05, "is_byok": False},
}
RECORDED_CALL = {
    "generation_id": RECORDED_COMPLETION["id"],
    "input_tokens": 9,
    "output_tokens": 5,
    "cost_usd": 3.4e-05,
    "latency_ms": 0,
}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_the_bridge_records_each_judge_call_with_the_price_the_endpoint_reported(monkeypatch, asynchronous):
    """2026-11-17: the bridge reported no price, so every Online-Mind2Web trial kept its full budget hold and half the
    smoke was refused; the judge's spend was only visible on the key."""
    completions = importlib.import_module("openai.resources.chat.completions")
    for cls in (completions.Completions, completions.AsyncCompletions):
        monkeypatch.setattr(cls, "create", cls.create)  # restored after the test
    calls: list[dict[str, object]] = []
    official_bridge._meter(calls)
    reply = httpx.MockTransport(lambda _request: httpx.Response(200, json=RECORDED_COMPLETION))
    message = [{"role": "user", "content": "Reply with OK"}]
    if asynchronous:
        client = openai.AsyncOpenAI(
            api_key="test", base_url="https://judge.invalid/v1", http_client=httpx.AsyncClient(transport=reply)
        )
        asyncio.run(client.chat.completions.create(model="openai/gpt-6.1-terra", messages=message))
    else:
        client = openai.OpenAI(
            api_key="test", base_url="https://judge.invalid/v1", http_client=httpx.Client(transport=reply)
        )
        client.chat.completions.create(model="openai/gpt-6.1-terra", messages=message)
    assert [{**call, "latency_ms": 0} for call in calls] == [RECORDED_CALL]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_a_retried_judge_call_leaves_the_judge_cost_unknown(monkeypatch, asynchronous):
    """The SDK retries inside `create`, and the first attempt may already have been billed, so the price the final
    response reports is not the whole spend."""
    completions = importlib.import_module("openai.resources.chat.completions")
    base = importlib.import_module("openai._base_client")
    for cls, name in ((completions.Completions, "create"), (completions.AsyncCompletions, "create"),
                      (base.SyncAPIClient, "_retry_request"), (base.AsyncAPIClient, "_retry_request")):  # fmt: skip
        monkeypatch.setattr(cls, name, getattr(cls, name))  # restored after the test
    calls: list[dict[str, object]] = []
    official_bridge._meter(calls)
    replies = iter((httpx.Response(500, json={}), httpx.Response(200, json=RECORDED_COMPLETION)))
    reply = httpx.MockTransport(lambda _request: next(replies))
    message = [{"role": "user", "content": "Reply with OK"}]
    if asynchronous:
        http = httpx.AsyncClient(transport=reply)
        client = openai.AsyncOpenAI(
            api_key="test", base_url="https://judge.invalid/v1", http_client=http, max_retries=1
        )
        asyncio.run(client.chat.completions.create(model="openai/gpt-6.1-terra", messages=message))
    else:
        http = httpx.Client(transport=reply)
        client = openai.OpenAI(api_key="test", base_url="https://judge.invalid/v1", http_client=http, max_retries=1)
        client.chat.completions.create(model="openai/gpt-6.1-terra", messages=message)
    assert [call["cost_usd"] for call in calls] == [None, RECORDED_CALL["cost_usd"]]
    judgment = official._judgment(official.MIND2WEB_SPEC, "openai/gpt-6.1-terra", {}, calls)
    assert judgment is not None and judgment.cost()[1] == 1  # one unpriced attempt: the budget keeps the hold


@pytest.mark.asyncio
async def test_a_bridge_that_fails_leaves_the_judge_cost_unknown(monkeypatch):
    row = {"task_id": "row-1", "confirmed_task": "Complete the task", "website": "https://example.test/start"}
    task = task_from_record(official.ONLINE_MIND2WEB, official._record(official.MIND2WEB_SPEC, row, 0, set()))
    page = {"url": "https://example.test/done", "title": "Done", "text": "Finished", "screenshot_png_b64": "iVBORw=="}
    request = GradeRequest(
        task=task,
        evidence=None,
        answers=("Finished",),
        product={"browser": {"final_page": page, "answer": "Finished", "steps": [], "pages": []}},
        execution={},
        harness_model=None,
        panel=JudgePanel(models=("configured/model",)),
        judge_config=JudgeConfig(base_url="https://judge.invalid/v1", api_key="test"),
        client=httpx.AsyncClient(),
    )

    async def bridge(_spec, _request, _payload):
        raise official.OfficialSuiteError("judge bridge exited 1")

    monkeypatch.setattr(official, "_bridge", bridge)
    monkeypatch.setattr(
        official,
        "_config",
        lambda _request, _spec: official._Config(
            root=task.path, python=task.path / "python", base_url="https://judge.invalid/v1", api_key="test"
        ),
    )
    graded = await official._grade(official.MIND2WEB_SPEC, request)
    await request.client.aclose()
    assert graded.checks[0].passed is None
    assert graded.unpriced_calls == 1  # it may have reached the judge before failing, so the budget keeps the hold


@pytest.mark.asyncio
async def test_a_judge_that_cannot_start_leaves_the_judge_cost_known(monkeypatch):
    monkeypatch.delenv("PARALLAX_OFFICIAL_ONLINE_JUDGE_MODEL", raising=False)
    row = {"task_id": "row-1", "confirmed_task": "Complete the task", "website": "https://example.test/start"}
    task = task_from_record(official.ONLINE_MIND2WEB, official._record(official.MIND2WEB_SPEC, row, 0, set()))
    page = {"url": "https://example.test/done", "title": "Done", "text": "Finished", "screenshot_png_b64": "iVBORw=="}
    request = GradeRequest(
        task=task,
        evidence=None,
        answers=("Finished",),
        product={"browser": {"final_page": page, "answer": "Finished", "steps": [], "pages": []}},
        execution={},
        harness_model=None,
        panel=None,
        judge_config=None,
        client=httpx.AsyncClient(),
    )
    graded = await official._grade(official.MIND2WEB_SPEC, request)
    await request.client.aclose()
    assert graded.checks[0].passed is None and "official judge unavailable" in graded.checks[0].detail
    assert graded.unpriced_calls == 0  # nothing reached the upstream judge, so the budget can release the hold


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


def test_a_bridge_failure_names_its_cause_without_the_key() -> None:
    exc = RuntimeError("401 from https://judge.example: key sk-secret\nis invalid")
    assert failure(exc, "sk-secret") == "RuntimeError: 401 from https://judge.example: key [redacted] is invalid"


def test_the_judge_interpreter_is_launched_through_its_venv_link(tmp_path, monkeypatch):
    """2026-11-17: resolving `.venv/bin/python` launched the base interpreter, which never reads the venv's
    `pyvenv.cfg`, so every Online-Mind2Web judgment failed on `No module named 'openai'`."""
    link = tmp_path / ".venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(sys.executable)
    monkeypatch.setenv("PARALLAX_OFFICIAL_ONLINE_MIND2WEB_ROOT", str(tmp_path))
    monkeypatch.delenv("PARALLAX_OFFICIAL_ONLINE_MIND2WEB_JUDGE_PYTHON", raising=False)
    monkeypatch.delenv("PARALLAX_OFFICIAL_JUDGE_PYTHON", raising=False)
    request = SimpleNamespace(judge_config=JudgeConfig(base_url="https://judge.invalid/v1", api_key="test"))
    config = official._config(request, official.MIND2WEB_SPEC)
    assert config.python == tmp_path.resolve() / ".venv" / "bin" / "python"
    assert config.python.is_symlink()


def test_the_bridge_writes_only_its_result_to_stdout_when_the_upstream_judge_prints(monkeypatch, capsys):
    def judge(request):
        print("Error processing response: list index out of range")
        return {"passed": False}

    monkeypatch.setattr(official_bridge, "_root", lambda request: None)
    monkeypatch.setattr(official_bridge, "_meter", lambda calls: None)
    monkeypatch.setattr(official_bridge, "_judge_mind2web", judge)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"operation": "judge", "suite": "online-mind2web"})))
    official_bridge.main()
    out = capsys.readouterr()
    assert json.loads(out.out) == {"passed": False, "calls": []}
    assert "Error processing response" in out.err
