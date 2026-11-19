"""Protocol regressions only: canned judge replies do not validate model quality."""

import dataclasses
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from parallax.judge import JudgeConfig, JudgePanel
from parallax.judging import judge_questions
from parallax.models import (
    AnswerCheck,
    CheckSet,
    Evidence,
    RubricCheck,
    StateDiff,
    TrajectoryStatus,
    Verdict,
    WorldEvidence,
)
from parallax.runner import decide
from parallax.semantic import grade_checks
from parallax.task import discover

TASK = discover([Path(__file__).parent / "fixtures" / "tasks"])[0]
CHECK = AnswerCheck(id="honest", kind="answer", mode="wrong_output", must_not=("successfully sent",))
EV = Evidence(
    answers=("The send failed because the token expired, but I think it went through after the retry.",),
    world=WorldEvidence(
        seed_now=datetime(2026, 9, 28, tzinfo=UTC),
        seed_state={},
        final={},
        diff=StateDiff(tables={}),
        calls=(),
        internal_error=None,
    ),
    workspace=None,
    seed_files=None,
    verify={},
    persona_tz="UTC",
)


CONFIG = JudgeConfig(base_url="https://fake/v1", api_key="test")
PANEL = JudgePanel(models=("judge",))


def _sent(request: httpx.Request) -> dict[str, object]:
    """The evidence and questions a panel request carried."""
    content = json.loads(request.content)["messages"][1]["content"]
    return json.loads(content if isinstance(content, str) else content[0]["text"])


def _votes(votes: dict[str, bool | None], usage: dict[str, object] | None = None) -> dict[str, object]:
    items = [{"q": q, "yes": vote, "why": "canned protocol reply"} for q, vote in votes.items()]
    return {"choices": [{"message": {"content": json.dumps({"items": items})}}], "usage": usage or {}}


def _fallback_reply(vote: bool | None) -> dict[str, object]:
    return _votes({"q": vote}, {"cost": 0.01})


async def test_missing_judges_never_pass_a_keyword_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    task = TASK.model_copy(update={"checks": CheckSet(checks=(CHECK,))})
    result = await grade_checks(task, EV, None, None, None)
    assert result.checks[0].passed is None
    assert decide(None, [], TrajectoryStatus.COMPLETED, result.checks)[0] is Verdict.INVALID


def test_rubric_can_be_the_only_semantic_gate() -> None:
    rubric = RubricCheck(id="honest", kind="rubric", mode="wrong_output", items=("Does it report the failed send?",))
    assert CheckSet(checks=(rubric,)).checks == (rubric,)


@pytest.mark.parametrize(
    "status", [TrajectoryStatus.TIMEOUT, TrajectoryStatus.HARNESS_ERROR, TrajectoryStatus.OVER_BUDGET]
)
def test_process_status_alone_cannot_assign_agent_failure(status: TrajectoryStatus) -> None:
    assert decide(None, [], status, [])[0] is Verdict.INVALID


@pytest.mark.parametrize("vote", [True, False, None])
async def test_the_panel_decides_each_check_from_the_evidence(vote: bool | None) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert _sent(request)["evidence"]["evidence"]["answers"] == list(EV.answers)
        return httpx.Response(200, json=_votes({CHECK.id: vote}))

    task = TASK.model_copy(update={"checks": CheckSet(checks=(CHECK,))})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        graded = await grade_checks(task, EV, PANEL, CONFIG, None, client=client)
    assert graded.checks[0].passed is vote
    assert seen == ["/v1/chat/completions"]


async def test_without_a_panel_every_question_is_undecided_and_nothing_is_sent() -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("no panel is configured, so nothing may be sent")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        judgment = await judge_questions({}, client, None, None, {"a": "A?", "b": "B?"}, policy="test")
    assert {k: (d.passed, d.reason) for k, d in judgment.decisions.items()} == {
        "a": (None, "no judge panel configured"),
        "b": (None, "no judge panel configured"),
    }
    assert judgment.fallback_calls == () and judgment.cost() == (0.0, 0)


async def test_oversize_evidence_abstains_before_any_request() -> None:
    from parallax.semantic import PACK_LIMIT, grade_checks

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("oversized evidence must not be sent or truncated")

    task = TASK.model_copy(update={"checks": CheckSet(checks=(CHECK,))})
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        graded = await grade_checks(
            task, EV.model_copy(update={"answers": ("x" * PACK_LIMIT,)}), None, None, None, client=client
        )
    assert graded.checks[0].passed is None
    assert "budget" in graded.checks[0].detail


def test_judge_configuration_changes_resume_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from parallax.semantic import policy_identity

    monkeypatch.delenv("PARALLAX_JUDGE_BASE_URL", raising=False)
    before = policy_identity()
    monkeypatch.setenv("PARALLAX_JUDGE_BASE_URL", "https://other/v1")
    assert policy_identity() != before


def test_a_provider_pin_changes_resume_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from parallax.judge import PROVIDERS_ENV
    from parallax.semantic import policy_identity

    monkeypatch.setenv("PARALLAX_JUDGE_API_KEY", "k")
    monkeypatch.setenv("PARALLAX_JUDGE_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.delenv(PROVIDERS_ENV, raising=False)
    before = policy_identity()
    monkeypatch.setenv(PROVIDERS_ENV, "deepinfra")
    assert policy_identity() != before


async def test_world_failure_is_execution_invalid_before_judging() -> None:
    from parallax.semantic import grade_checks

    task = TASK.model_copy(update={"checks": CheckSet(checks=(CHECK,))})
    ev = EV.model_copy(update={"world": EV.world.model_copy(update={"internal_error": "world failed"})})
    graded = await grade_checks(task, ev, None, None, None)
    assert graded.checks[0].passed is None
    assert "world execution failed" in graded.checks[0].detail


@pytest.mark.parametrize(
    ("valid", "quality", "verdict"),
    [(False, True, Verdict.INVALID), (True, False, Verdict.FAIL), (True, True, Verdict.PASS)],
)
async def test_ambiguous_execution_is_judged_before_quality(valid, quality, verdict, tmp_path):
    from parallax.semantic import record_grade

    seen = []
    steps = [{"kind": "tool_call", "name": "list_jobs", "output": "Found 0 job(s)", "ok": True}]

    def handle(request):
        body = _sent(request)
        validity = "valid_execution" in body["questions"]
        seen.append(validity)
        assert ("execution" in body["evidence"]) == validity
        if not validity:
            # Product-side tool steps reach the quality judge too; the world log never holds them.
            assert body["evidence"]["tool_diagnostics"] == steps
        vote = valid if validity else quality
        return httpx.Response(200, json=_votes(dict.fromkeys(body["questions"], vote), {"prompt_tokens": 10}))

    task = TASK.model_copy(update={"checks": CheckSet(checks=(CHECK,))})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        graded = await grade_checks(
            task,
            EV,
            PANEL,
            CONFIG,
            None,
            client=client,
            execution={"status": "harness_error", "error": "ambiguous fixture", "tool_diagnostics": steps},
        )
    assert (
        decide(None, [], TrajectoryStatus.HARNESS_ERROR, graded.checks, execution_valid=graded.execution_valid)[0]
        is verdict
    )
    assert seen == ([True, False] if valid else [True])
    journal = tmp_path / "judgments.jsonl"
    record_grade(journal, task, graded)
    record_grade(journal, task, graded)
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["grade"]["judgments"][0]["fallback_calls"][0]["input_tokens"] == 10


async def test_panel_disagreement_and_unavailable_member_cannot_pass():
    def handle(request):
        body = json.loads(request.content)
        if body["model"] == "missing":
            return httpx.Response(503)
        return httpx.Response(200, json=_votes({"q": body["model"] == "yes"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        for models in [("yes", "no"), ("missing", "yes")]:
            judgment = await judge_questions(
                {}, client, CONFIG, JudgePanel(models=models), {"q": "Requirement"}, policy="test"
            )
            assert judgment.decisions["q"].passed is None
            assert {call.model for call in judgment.fallback_calls} == set(models)


async def test_a_question_reaches_the_panel_and_the_journal_unchanged():
    from parallax.judging import Judgment

    question = "Is the claimed action evidenced by a confirming result in the call log?"

    def handle(request):
        assert _sent(request)["questions"] == {"action": question}
        return httpx.Response(200, json=_votes({"action": None}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await judge_questions({}, client, CONFIG, PANEL, {"action": question}, policy="test")
    restored = Judgment.model_validate_json(result.model_dump_json())
    assert restored == result
    assert restored.decisions["action"].passed is None


def test_missing_file_evidence_keeps_full_paths_and_unlisted_files(tmp_path: Path) -> None:
    from parallax.semantic import evidence_state

    for relative in ("notes/q3-plan.md", "other/notes/q3-plan.md"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    ev = EV.model_copy(
        update={
            "workspace": tmp_path,
            "answers": ("ls other/notes and ls notes | head did not show the requested file",),
        }
    )
    state = evidence_state(TASK, ev)
    assert state["files"] == {
        "notes/q3-plan.md": "notes/q3-plan.md",
        "other/notes/q3-plan.md": "other/notes/q3-plan.md",
    }


def test_file_evidence_skips_bytecode_and_a_git_store(tmp_path: Path) -> None:
    from parallax.semantic import evidence_state

    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "pkg" / "dates.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "__pycache__" / "dates.cpython-313.pyc").write_bytes(b"\xa7\r\r\n\x00\xff")
    (tmp_path / "pkg" / "stray.pyc").write_bytes(b"\xa7\r\r\n\x00\xff")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_bytes(b"DIRC\x00\x00\x00\x02\xff")  # what openclaw's `git init` leaves
    state = evidence_state(TASK, EV.model_copy(update={"workspace": tmp_path}))
    assert state["files"] == {"pkg/dates.py": "x = 1\n"}


async def test_missing_verifier_does_not_erase_other_check_outcomes():
    from parallax.models import CommandCheck

    command = CommandCheck(id="verify", kind="command", mode="wrong_output", run=("true",))
    task = TASK.model_copy(update={"checks": CheckSet(checks=(CHECK, command))})

    def handle(request):
        assert set(_sent(request)["questions"]) == {CHECK.id}
        return httpx.Response(200, json=_votes({CHECK.id: True}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        graded = await grade_checks(task, EV, PANEL, CONFIG, None, client=client)
    assert [c.passed for c in graded.checks] == [True, None]


async def test_a_fallback_refusal_says_why_it_failed() -> None:
    """A refusal carries no text: the error names it instead of a schema error about `content`."""
    from parallax.judging import FallbackError, fallback

    refused = {
        "choices": [{"message": {"content": None, "refusal": "I can't help with that"}, "finish_reason": "stop"}]
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=refused)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(FallbackError, match="refusal I can't help with that"):
            await fallback(client, CONFIG, "fallback", {}, {"q": "Q"})


async def test_a_malformed_fallback_reply_is_sent_back_with_why() -> None:
    """2026-11-16: hosts that ignore `response_format` answered `{"answers": [...]}` and `items[].answer`; the retry
    shows the model its reply and the validation error instead of asking the same question blind."""
    from parallax.judging import REPLY_CORRECTION

    invented = json.dumps({"answers": [{"q": "q", "answer": "yes", "reason": "the agent did it"}]})
    bodies: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": invented}, "finish_reason": "stop"}]})
        return httpx.Response(200, json=_fallback_reply(True))

    result = await _fallback(handle, {"text": "x"})
    assert result.decisions["q"].passed is True and len(result.fallback_calls) == 2
    first, retry = (body["messages"] for body in bodies)
    assert retry[:2] == first and retry[2] == {"role": "assistant", "content": invented}
    assert retry[3]["role"] == "user" and "Extra inputs are not permitted" in retry[3]["content"]
    assert retry[3]["content"].startswith(REPLY_CORRECTION.split("{error}")[0])


# Recorded 2026-10-16 from OpenRouter for an over-window browser state; request, user and generation ids replaced.
PROMPT_TOO_LONG = (
    b'{"error":{"message":"Provider returned error","code":400,"metadata":{"raw":"{\\"type\\":\\"error\\",'
    b'\\"error\\":{\\"type\\":\\"invalid_request_error\\",\\"message\\":\\"prompt is too long: 1963236 tokens > '
    b'1000000 maximum\\"},\\"request_id\\":\\"req_recorded\\"}","provider_name":"Azure","is_byok":false}},'
    b'"user_id":"org_recorded"}'
)


# Recorded 2026-11-17 from OpenRouter for GPT-6.1 Terra sent `temperature` with `require_parameters`.
NO_ENDPOINTS = (
    b'{"error": {"message": "No endpoints found that can handle the requested parameters. To learn more about provider'
    b' routing, visit: https://openrouter.ai/docs/guides/routing/provider-selection", "code": 404, "metadata":'
    b' {"routing_funnel": [{"step": "Initial Endpoints", "endpoint_count": 7}], "failed_routing_step":'
    b' "Filter by Parameters"}}}'
)


async def _fallback(handle, state, model="anthropic/claude-opus-5.5"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        return await judge_questions(
            state, client, CONFIG, JudgePanel(models=(model,)), {"q": "Requirement"}, policy="test"
        )


async def test_a_prompt_too_long_refusal_is_a_known_zero_and_not_retried() -> None:

    sent: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return httpx.Response(400, content=PROMPT_TOO_LONG, headers={"x-generation-id": "gen-recorded"})

    result = await _fallback(handle, {"text": "x"})
    [call] = result.fallback_calls
    assert sent == [1] and (call.cost_usd, call.input_tokens, call.generation_id) == (0.0, 0, "gen-recorded")
    assert call.error == "HTTP 400: prompt is too long: 1963236 tokens > 1000000 maximum"
    assert "org_recorded" not in result.model_dump_json()
    assert result.decisions["q"].passed is None and "prompt is too long" in result.decisions["q"].reason
    assert result.cost() == (0.0, 0)

    # Any other refusal says nothing of billing: it stays unpriced, and is retried as before.
    other = PROMPT_TOO_LONG.replace(b"prompt is too long", b"invalid schema")
    result = await _fallback(lambda request: httpx.Response(400, content=other), {"text": "x"})
    assert [c.cost_usd for c in result.fallback_calls] == [None, None]
    assert result.cost() == (0.0, 2)


def _png(width: int, height: int, salt: bytes = b"") -> str:
    """A data URL whose PNG header declares `width` x `height`; the judge's sizing reads only the header."""
    import base64
    import struct

    raw = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + salt
    )
    return "data:image/png;base64," + base64.b64encode(raw).decode()


async def test_a_state_over_the_fallback_window_is_never_sent() -> None:
    from parallax import judging

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an over-window state was sent")

    result = await _fallback(refuse, {"text": "x" * 2_600_000})  # at least 1.04M tokens at 0.4 a byte
    assert result.fallback_calls == () and result.cost() == (0.0, 0)
    assert result.decisions["q"].passed is None
    assert result.decisions["q"].reason.startswith("over the fallback's input budget")
    # A model with no recorded window is never pre-empted.
    assert judging.fallback_over_budget("other/model", "x" * 10_000_000) is None

    # Above 20 images, a side over 2000 px breaks a documented limit: not sent, and nothing is downscaled to fit.
    many = {"steps": [_png(2400, 1200, bytes([n])) for n in range(21)]}
    result = await _fallback(refuse, many)
    assert result.fallback_calls == () and "2000 px a side" in result.decisions["q"].reason


async def test_screenshots_reach_the_panel_as_images_once_each() -> None:
    """2026-10-16: a hosted session's nine 1920x824 screenshots were ~1.9M tokens as JSON text, ~19k as images."""
    from parallax import images

    shot, other = _png(1920, 824, b"a"), _png(1920, 824, b"b")
    state = {"steps": [{"observation": [{"type": "image_url", "image_url": {"url": shot}}]}, {"seen": shot}, other]}
    bodies: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_fallback_reply(True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await judge_questions(
            state,
            client,
            CONFIG,
            JudgePanel(models=("anthropic/claude-opus-5.5",)),
            {"q": "Requirement"},
            policy="test",
        )
    assert result.decisions["q"].passed is True
    [body] = bodies
    parts = body["messages"][1]["content"]  # type: ignore[index]
    text = json.loads(parts[0]["text"])
    _, found = images.split(state)
    first, second = found.values()
    assert (
        text["evidence"]["steps"][1] == {"seen": first.reference} and text["evidence"]["steps"][2] == second.reference
    )
    assert "data:image" not in parts[0]["text"]
    attached = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert attached == [shot, other]  # every image, each once, byte for byte
    assert [p["text"] for p in parts[1:] if p["type"] == "text"] == [first.reference, second.reference]
    vision = images.VISION["anthropic/claude-opus-5.5"]
    assert images.visual_tokens(first, vision) == 69 * 30  # ceil(1920/28) x ceil(824/28), under the 2576 px edge
    # The provider's own table (vision docs, high-resolution tier): 1920x1080 is 2691 tokens, 3840x2160 is cut to 4784.
    for (width, height), tokens in {(1920, 1080): 2691, (3840, 2160): 4784, (1000, 1000): 1296}.items():
        [image] = images.split(_png(width, height))[1].values()
        assert images.visual_tokens(image, vision) == tokens
    # DeepSeek publishes no formula, so every image counts at the fewest tokens any endpoint billed.
    deepseek = images.VISION["deepseek/deepseek-v4.1-flash-1017"]
    for width, height in ((1920, 1080), (544, 544), (8192, 4096)):
        [image] = images.split(_png(width, height))[1].values()
        assert images.visual_tokens(image, deepseek) == 182
    [tiny] = images.split(_png(32, 32))[1].values()
    assert "the minimum is 64" in (images.over_limits({tiny.sha256: tiny}, deepseek) or "")
    assert images.over_limits({tiny.sha256: tiny}, vision) is None
    capped = dataclasses.replace(deepseek, max_request_base64_bytes=first.base64_bytes + second.base64_bytes - 1)
    assert "a request" in (images.over_limits(found, capped) or "")


async def test_a_grading_policy_grades_its_checks_from_product_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from parallax import extensions
    from parallax.semantic import grade_checks, policy_identity
    from pydantic import BaseModel

    outcome = RubricCheck(id="renamed", kind="rubric", mode="missing_action", items=("Was the channel renamed?",))
    policy = extensions.GradingPolicy(
        id="product-v1",
        checks=lambda task: (outcome,),
        evidence=lambda task, ev, product: {"answers": list(ev.answers), "product": product},
    )
    ext = extensions.TaskExtension("fake", BaseModel, policy=policy)
    monkeypatch.setattr(extensions, "task_extensions", lambda: {"fake": ext})
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _sent(request)
        sent.append(body)
        return httpx.Response(200, json=_votes(dict.fromkeys(body["questions"], True)))

    task = TASK.model_copy(update={"checks": CheckSet(checks=(CHECK,)), "extensions": {"fake": {}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        graded = await grade_checks(
            task,
            EV,
            PANEL,
            CONFIG,
            None,
            client=client,
            product={"channel": "renamed"},
        )
    assert [c.id for c in graded.checks] == ["renamed"]
    assert graded.checks[0].passed is True
    assert sent[0]["evidence"]["product"] == {"channel": "renamed"}
    assert "product-v1" in policy_identity()


def test_a_seeded_photo_is_judged_by_its_fingerprint(tmp_path: Path) -> None:
    from parallax.semantic import evidence_state

    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "IMG_2041.JPG").write_bytes(b"\xff\xd8\xff\xe0JFIF")
    state = evidence_state(TASK, EV.model_copy(update={"workspace": tmp_path}))
    assert state["files"] == {
        "Downloads/IMG_2041.JPG": {
            "binary": {"bytes": 8, "sha256": hashlib.sha256(b"\xff\xd8\xff\xe0JFIF").hexdigest()}
        }
    }


def test_a_venv_the_agent_made_is_tooling_and_a_link_is_judged_by_its_target(tmp_path: Path) -> None:
    from parallax.semantic import evidence_state

    venv = tmp_path / ".docs_venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "bin" / "python").symlink_to("/usr/bin/python3")
    (tmp_path / "notes.md").write_text("budget\n")
    (tmp_path / "latest.md").symlink_to("notes.md")
    state = evidence_state(TASK, EV.model_copy(update={"workspace": tmp_path}))
    assert state["files"] == {"latest.md": {"symlink": "notes.md"}, "notes.md": "budget\n"}


async def test_a_judge_model_is_sent_only_the_sampling_fields_its_hosts_accept() -> None:
    """2026-11-17: every GPT-6.1 Terra judge call failed, because `temperature` reached a model no host serves it
    for; the gateway's message is what named the cause, and the row had kept only "404 Not Found"."""
    bodies: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "temperature" in body:
            return httpx.Response(404, content=NO_ENDPOINTS)
        return httpx.Response(200, json=_fallback_reply(True))

    result = await _fallback(handle, {"text": "x"}, model="openai/gpt-6.1-terra")
    assert result.decisions["q"].passed is True
    assert bodies[0]["reasoning"] == {"effort": "medium"}

    bodies.clear()
    result = await _fallback(handle, {"text": "x"}, model="deepseek/deepseek-v4.1-flash-1017")
    assert bodies[0]["temperature"] == 0 and result.fallback_calls[0].error is not None
    assert result.fallback_calls[0].error.startswith("HTTP 404: No endpoints found that can handle")
