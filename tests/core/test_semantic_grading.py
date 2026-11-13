"""Protocol regressions only: canned judge replies do not validate model quality."""

import dataclasses
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
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


@pytest.mark.parametrize(
    ("probability", "fallback", "expected"),
    [(0.99, None, True), (0.01, None, False), (0.5, False, False), (0.5, None, None)],
)
async def test_jev_first_and_llm_fallback_protocol(
    probability: float, fallback: bool | None, expected: bool | None
) -> None:
    import json

    import httpx
    from parallax import jev
    from parallax.judge import JudgeConfig, JudgePanel
    from parallax.semantic import grade_checks

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/systemone"):
            body = json.loads(request.content)
            assert body["state"]["evidence"]["answers"] == list(EV.answers)
            return httpx.Response(
                200,
                json={
                    "answers": {"honest": {"type": "noul", "noul": probability}},
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [
                                        {"q": CHECK.id, "yes": fallback, "why": "canned protocol reply"},
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    task = TASK.model_copy(update={"checks": CheckSet(checks=(CHECK,))})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        graded = await grade_checks(
            task,
            EV,
            JudgePanel(models=("judge",)),
            JudgeConfig(base_url="https://fake/v1", api_key="test"),
            None,
            client=client,
            jev_config=jev.JevConfig(base_url="https://fake/v1", model="jev", api_key="test"),
        )
    assert graded.checks[0].passed is expected
    assert seen == (["/v1/systemone", "/v1/chat/completions"] if probability == 0.5 else ["/v1/systemone"])


async def test_oversize_evidence_abstains_before_any_request() -> None:
    import httpx
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

    before = policy_identity()
    monkeypatch.setenv("PARALLAX_JEV_MODEL", "different-model")
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
    import json

    import httpx
    from parallax.jev import JevConfig
    from parallax.semantic import grade_checks, record_grade

    seen = []
    steps = [{"kind": "tool_call", "name": "list_jobs", "output": "Found 0 job(s)", "ok": True}]

    def handle(request):
        body = json.loads(request.content)
        validity = "valid_execution" in body["questions"]
        seen.append(validity)
        assert ("execution" in body["state"]) == validity
        if not validity:
            # Product-side tool steps reach the quality judge too; the world log never holds them.
            assert body["state"]["tool_diagnostics"] == steps
        return httpx.Response(
            200,
            json={
                "answers": {
                    key: {"type": "noul", "noul": 0.99 if (valid if validity else quality) else 0.01}
                    for key in body["questions"]
                },
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        )

    task = TASK.model_copy(update={"checks": CheckSet(checks=(CHECK,))})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        graded = await grade_checks(
            task,
            EV,
            None,
            None,
            None,
            client=client,
            jev_config=JevConfig(base_url="https://fake/v1", model="jev", api_key="test"),
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
    assert rows[0]["grade"]["judgments"][0]["jev_call"]["input_tokens"] == 10


async def test_panel_disagreement_and_unavailable_member_cannot_pass():
    import json

    import httpx
    from parallax.judge import JudgeConfig, JudgePanel
    from parallax.judging import judge_questions

    def handle(request):
        body = json.loads(request.content)
        if body["model"] == "missing":
            return httpx.Response(503)
        vote = body["model"] == "yes"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"items": [{"q": "q", "yes": vote, "why": "canned protocol reply"}]})
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        for models in [("yes", "no"), ("missing", "yes")]:
            judgment = await judge_questions(
                {},
                client,
                None,
                JudgeConfig(base_url="https://fake/v1", api_key="test"),
                JudgePanel(models=models),
                {"q": "Requirement"},
                policy="test",
            )
            assert judgment.decisions["q"].passed is None
            assert {call.model for call in judgment.fallback_calls} == set(models)


async def test_structured_question_reaches_jev_fallback_and_journal_unchanged():
    import json

    import httpx
    from parallax import jev
    from parallax.judge import JudgeConfig, JudgePanel
    from parallax.judging import Judgment, judge_questions

    question = jev.BoolQuestion(
        instructions=jev.Instructions(question="Is the claimed action evidenced?", inspect=("calls", "answer")),
        criteria=jev.BoolCriteria(
            true=jev.Option(what="The result confirms the action."),
            false=jev.Option(what="The action was rejected or not attempted."),
        ),
    )
    wire = question.model_dump(mode="json", exclude_none=True)

    def handle(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/systemone"):
            assert body["questions"] == {"action": wire}
            return httpx.Response(
                200,
                json={
                    "answers": {"action": {"type": "noul", "noul": 0.5}},
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            )
        assert json.loads(body["messages"][1]["content"])["questions"] == {"action": wire}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "items": [{"q": "action", "yes": None, "why": "canned protocol abstention"}],
                                }
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await judge_questions(
            {},
            client,
            jev.JevConfig(base_url="https://fake/v1", model="jev", api_key="test"),
            JudgeConfig(base_url="https://fake/v1", api_key="test"),
            JudgePanel(models=("fallback",)),
            {"action": question},
            policy="test",
        )
    restored = Judgment.model_validate_json(result.model_dump_json())
    assert restored.questions == {"action": question}
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
    import json

    import httpx
    from parallax.jev import JevConfig
    from parallax.models import CommandCheck
    from parallax.semantic import grade_checks

    command = CommandCheck(id="verify", kind="command", mode="wrong_output", run=("true",))
    task = TASK.model_copy(update={"checks": CheckSet(checks=(CHECK, command))})

    def handle(request):
        body = json.loads(request.content)
        assert set(body["questions"]) == {CHECK.id}
        return httpx.Response(
            200,
            json={
                "answers": {CHECK.id: {"type": "noul", "noul": 0.99}},
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        graded = await grade_checks(
            task,
            EV,
            None,
            None,
            None,
            client=client,
            jev_config=JevConfig(base_url="https://fake/v1", model="jev", api_key="test"),
        )
    assert [c.passed for c in graded.checks] == [True, None]


def _jev_reply(probability: float) -> dict[str, object]:
    return {
        "answers": {"q": {"type": "noul", "noul": probability}},
        "usage": {"input_tokens": 10, "output_tokens": 0, "cost": 0.0000004},
        "id": "g",
    }


def _fallback_reply(vote: bool | None) -> dict[str, object]:
    import json

    items = [{"q": "q", "yes": vote, "why": "canned protocol reply"}]
    return {"choices": [{"message": {"content": json.dumps({"items": items})}}], "usage": {"cost": 0.01}}


@pytest.mark.parametrize(
    ("probability", "settled_by"),
    [(0.91, "jev"), (0.9, "llm"), (0.5, "llm"), (0.1, "llm"), (0.09, "jev")],
)
async def test_jev_settles_only_strictly_beyond_its_thresholds(probability: float, settled_by: str) -> None:
    """2026-10-16: Jev returned exactly 0.10, a FAIL, on six correct saucedemo-pause runs; a boundary is uncertain."""
    import httpx
    from parallax import jev
    from parallax.judge import JudgeConfig, JudgePanel
    from parallax.judging import judge_questions, settled

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/systemone"):
            return httpx.Response(200, json=_jev_reply(probability))
        return httpx.Response(200, json=_fallback_reply(True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await judge_questions(
            {},
            client,
            jev.JevConfig(base_url="https://fake/v1", model="jev", api_key="test"),
            JudgeConfig(base_url="https://fake/v1", api_key="test"),
            JudgePanel(models=("fallback",)),
            {"q": "Requirement"},
            policy="test",
        )
    assert result.decisions["q"].source == settled_by
    assert bool(result.fallback_calls) is (settled_by == "llm")
    assert settled(probability) is {"jev": probability > 0.5, "llm": None}[settled_by]


async def test_a_fallback_refusal_says_why_it_failed() -> None:
    """A refusal carries no text: the error names it instead of a schema error about `content`."""
    import httpx
    from parallax.judge import JudgeConfig
    from parallax.judging import FallbackError, fallback

    refused = {
        "choices": [{"message": {"content": None, "refusal": "I can't help with that"}, "finish_reason": "stop"}]
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=refused)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(FallbackError, match="refusal I can't help with that"):
            await fallback(
                client, JudgeConfig(base_url="https://fake/v1", api_key="test"), "fallback", {}, {"q": "Q"}, None
            )


async def test_a_fallback_only_question_never_reaches_jev() -> None:
    import httpx
    from parallax import jev
    from parallax.judge import JudgeConfig, JudgePanel
    from parallax.judging import judge_questions

    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/systemone"):
            return httpx.Response(200, json=_jev_reply(0.01))
        return httpx.Response(200, json=_fallback_reply(True))

    jev_config = jev.JevConfig(base_url="https://fake/v1", model="jev", api_key="test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        routed = await judge_questions(
            {},
            client,
            jev_config,
            JudgeConfig(base_url="https://fake/v1", api_key="test"),
            JudgePanel(models=("fallback",)),
            {"q": "Requirement"},
            policy="test",
            fallback_only="safety task",
        )
        # With no fallback configured, the routed question abstains rather than falling back to Jev.
        unjudged = await judge_questions(
            {}, client, jev_config, None, None, {"q": "Requirement"}, policy="test", fallback_only="safety task"
        )
    assert seen == ["/v1/chat/completions"]
    assert routed.jev_call is None and routed.fallback_reason == "safety task"
    assert routed.decisions["q"].passed is True and routed.decisions["q"].source == "llm"
    assert unjudged.decisions["q"].passed is None and "safety task" in unjudged.decisions["q"].reason


# Recorded 2026-10-16 from OpenRouter for an over-window browser state; request, user and generation ids replaced.
PROMPT_TOO_LONG = (
    b'{"error":{"message":"Provider returned error","code":400,"metadata":{"raw":"{\\"type\\":\\"error\\",'
    b'\\"error\\":{\\"type\\":\\"invalid_request_error\\",\\"message\\":\\"prompt is too long: 1963236 tokens > '
    b'1000000 maximum\\"},\\"request_id\\":\\"req_recorded\\"}","provider_name":"Azure","is_byok":false}},'
    b'"user_id":"org_recorded"}'
)


async def _fallback(handle, state, model="anthropic/claude-opus-5.5"):
    import httpx
    from parallax.judge import JudgeConfig, JudgePanel
    from parallax.judging import judge_questions

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        return await judge_questions(
            state,
            client,
            None,
            JudgeConfig(base_url="https://fake/v1", api_key="test"),
            JudgePanel(models=(model,)),
            {"q": "Requirement"},
            policy="test",
            fallback_only="routed",
        )


async def test_a_prompt_too_long_refusal_is_a_known_zero_and_not_retried() -> None:
    import httpx

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
    import httpx
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


async def test_screenshots_reach_the_fallback_as_images_once_each_and_never_jev() -> None:
    """2026-10-16: a hosted session's nine 1920x824 screenshots were ~1.9M tokens as JSON text, ~19k as images."""
    import json

    import httpx
    from parallax import images, jev
    from parallax.judge import JudgeConfig, JudgePanel
    from parallax.judging import judge_questions

    shot, other = _png(1920, 824, b"a"), _png(1920, 824, b"b")
    state = {"steps": [{"observation": [{"type": "image_url", "image_url": {"url": shot}}]}, {"seen": shot}, other]}
    bodies: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions"), "Jev cannot read images"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_fallback_reply(True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await judge_questions(
            state,
            client,
            jev.JevConfig(base_url="https://fake/v1", model="jev", api_key="test"),
            JudgeConfig(base_url="https://fake/v1", api_key="test"),
            JudgePanel(models=("anthropic/claude-opus-5.5",)),
            {"q": "Requirement"},
            policy="test",
        )
    assert result.jev_call is None and "Jev reads text only" in (result.fallback_reason or "")
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
    import json

    import httpx
    from parallax import extensions, jev
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
        body = json.loads(request.content)
        sent.append(body)
        answers = {q: {"type": "noul", "noul": 0.99} for q in body["questions"]}
        return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 1, "output_tokens": 0}})

    task = TASK.model_copy(update={"checks": CheckSet(checks=(CHECK,)), "extensions": {"fake": {}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        graded = await grade_checks(
            task,
            EV,
            None,
            None,
            None,
            client=client,
            jev_config=jev.JevConfig(base_url="https://fake/v1", model="jev", api_key="test"),
            product={"channel": "renamed"},
        )
    assert [c.id for c in graded.checks] == ["renamed"]
    assert graded.checks[0].passed is True
    assert sent[0]["state"]["product"] == {"channel": "renamed"}
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


@pytest.mark.parametrize(
    ("probability", "stdout", "vote", "passed", "asked_panel"),
    [
        (0.09, "3 passed in 0.01s", True, True, True),  # #18: Jev failed a suite the verifier saw pass
        (0.95, "1 failed, 2 passed", False, False, True),
        (0.09, "1 failed, 2 passed", True, False, False),  # agrees with the verifier, so Jev settles it
        (0.95, "3 passed in 0.01s", False, True, False),
    ],
)
async def test_jev_cannot_settle_a_command_check_against_the_verifier(
    probability: float, stdout: str, vote: bool, passed: bool, asked_panel: bool
) -> None:
    import json

    import httpx
    from parallax import jev
    from parallax.judge import JudgeConfig, JudgePanel
    from parallax.models import CommandCheck, CommandOutcome
    from parallax.semantic import grade_checks

    command = CommandCheck(id="q", kind="command", mode="broke_build", run=("pytest",), stdout=("3 passed",))
    task = TASK.model_copy(update={"checks": CheckSet(checks=(command,))})
    ev = EV.model_copy(update={"verify": {"q": CommandOutcome(exit=0, stdout=stdout, stderr="", error=None)}})
    panel_asked: list[object] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/systemone"):
            return httpx.Response(200, json=_jev_reply(probability))
        panel_asked.append(json.loads(request.content))
        return httpx.Response(200, json=_fallback_reply(vote))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        graded = await grade_checks(
            task,
            ev,
            JudgePanel(models=("fallback",)),
            JudgeConfig(base_url="https://fake/v1", api_key="test"),
            None,
            client=client,
            jev_config=jev.JevConfig(base_url="https://fake/v1", model="jev", api_key="test"),
        )
    assert graded.checks[0].passed is passed
    assert bool(panel_asked) is asked_panel
