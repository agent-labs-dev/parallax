"""Each parser against the fixture recorded (or, where marked synthetic, hand-built) for its harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from parallax.harnesses.parsers import PARSERS
from parallax.models import ParserId, StepKind, TurnOutput, TurnStatus

FIXTURES = Path(__file__).parent / "fixtures"


def parse(pid: ParserId, harness: str, turn: int | str) -> TurnOutput:
    name = f"turn{turn}" if isinstance(turn, int) else turn
    out = FIXTURES / harness / f"{name}.stdout"
    err = FIXTURES / harness / f"{name}.stderr"
    return PARSERS[pid](out.read_text(), err.read_text() if err.exists() else "")


def tool_calls(out: TurnOutput) -> list[str | None]:
    return [s.name for s in out.steps if s.kind is StepKind.TOOL_CALL]


def test_claude_turn_with_world_tool_call() -> None:
    out = parse(ParserId.CLAUDE_STREAM_JSON, "claude-code", 1)
    assert out.status is TurnStatus.COMPLETED
    assert out.answer == "Posted nonce-4471 to #conf."
    assert tool_calls(out) == ["chat_post_message"]
    call = next(s for s in out.steps if s.kind is StepKind.TOOL_CALL)
    assert call.args == {"channel": "#conf", "text": "nonce-4471"}
    assert out.session_id is not None
    assert out.actual_model == "claude-sonnet-4-5"


def test_claude_answer_keeps_text_written_between_tool_calls() -> None:
    out = parse(ParserId.CLAUDE_STREAM_JSON, "claude-code", "text-between-tools")
    messages = [s.output or "" for s in out.steps if s.kind is StepKind.MESSAGE]
    assert len(messages) == 3
    assert messages[1].startswith("Here's where the thread stands")
    assert out.answer == "\n\n".join(messages)


def test_claude_answer_excludes_sub_agent_text() -> None:
    lines = (FIXTURES / "claude-code" / "text-between-tools.stdout").read_text().splitlines()
    sub = json.loads(lines[4])  # a top-level text message, re-parented under a Task call
    sub["parent_tool_use_id"] = "toolu_task"
    sub["message"]["content"] = [{"type": "text", "text": "SUB-AGENT NOTES"}]
    out = PARSERS[ParserId.CLAUDE_STREAM_JSON]("\n".join([*lines[:-1], json.dumps(sub), lines[-1]]), "")
    assert "SUB-AGENT NOTES" in [s.output for s in out.steps]
    assert "SUB-AGENT NOTES" not in out.answer


def test_claude_resumed_turn_keeps_session() -> None:
    first = parse(ParserId.CLAUDE_STREAM_JSON, "claude-code", 1)
    second = parse(ParserId.CLAUDE_STREAM_JSON, "claude-code", 2)
    assert second.status is TurnStatus.COMPLETED
    assert second.session_id == first.session_id
    assert tool_calls(second) == []


def test_codex_turn_with_mcp_tool_call() -> None:
    out = parse(ParserId.CODEX_JSONL, "codex", 1)
    assert out.status is TurnStatus.COMPLETED
    assert out.answer == "Posted nonce-4471 to #conf."
    assert tool_calls(out) == ["chat_post_message"]
    assert out.session_id is not None


def test_codex_resume_reports_same_thread() -> None:
    assert parse(ParserId.CODEX_JSONL, "codex", 2).session_id == parse(ParserId.CODEX_JSONL, "codex", 1).session_id


def test_codex_failed_turn_is_harness_error() -> None:
    stdout = "\n".join(
        json.dumps(e)
        for e in [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.failed", "error": {"message": "upstream 500"}},
        ]
    )
    out = PARSERS[ParserId.CODEX_JSONL](stdout, "")
    assert out.status is TurnStatus.HARNESS_ERROR
    assert out.error is not None and "upstream 500" in out.error


def test_hermes_session_from_stderr_trailer() -> None:
    out = parse(ParserId.HERMES_QUIET, "hermes", 1)
    assert out.status is TurnStatus.COMPLETED
    assert out.answer == "Saved."
    assert out.session_id == "local-9a91dc1ad664e756f796ec3815887beafb2c52859fed55346e9fbfefb171fb43"
    assert parse(ParserId.HERMES_QUIET, "hermes", 2).session_id == out.session_id


def test_hermes_tool_steps_are_this_turns_calls_from_the_session_export() -> None:
    """#34: quiet output alone showed "Saved." with no memory call behind it."""
    first, second = parse(ParserId.HERMES_QUIET, "hermes", 1), parse(ParserId.HERMES_QUIET, "hermes", 2)
    assert tool_calls(first) == ["memory"]
    assert first.steps[0].args == {"action": "add", "target": "user", "content": "Allotment plot number: 27"}
    assert first.steps[0].output is not None and '"success": true' in first.steps[0].output
    assert tool_calls(second) == []  # the resumed session's earlier call belongs to turn 1


def test_hermes_without_its_session_export_is_error() -> None:
    out = PARSERS[ParserId.HERMES_QUIET]("Saved.\n", "session_id: s1\n")
    assert out.status is TurnStatus.HARNESS_ERROR


def test_hermes_without_session_trailer_is_error() -> None:
    out = PARSERS[ParserId.HERMES_QUIET]("an answer\n", "")
    assert out.status is TurnStatus.HARNESS_ERROR


def test_openclaw_answer_and_steps_come_from_the_transcript() -> None:
    out = parse(ParserId.OPENCLAW_JSON, "openclaw", 1)
    assert out.status is TurnStatus.COMPLETED
    # The envelope's payloads hold only the closing line; the text before each tool call is answer too.
    assert out.answer.startswith("I'll get the check-in code from #conf")
    assert out.answer.endswith("Done. Today's check-in code is **heron-e4e298**.")
    assert tool_calls(out) == ["chat_read_channel", "chat_post_message"]
    assert [s.ok for s in out.steps if s.kind is StepKind.TOOL_RESULT] == [True, True]
    assert out.actual_model == "claude-haiku-4-5-20251001"
    assert (out.usage[0].output, out.usage[0].requests) == (225, 3)


def test_openclaw_resumed_turn_skips_earlier_runs() -> None:
    first = parse(ParserId.OPENCLAW_JSON, "openclaw", 1)
    out = parse(ParserId.OPENCLAW_JSON, "openclaw", "resume")
    assert out.status is TurnStatus.COMPLETED
    assert out.answer == "The codeword is **amber-falcon-62**."
    assert tool_calls(out) == []
    assert out.session_id == "4f7b8bd1-5bd8-43f6-a2d3-e884464153fb" != first.session_id


def test_openclaw_error_envelope() -> None:
    out = parse(ParserId.OPENCLAW_JSON, "openclaw", "error")
    assert out.status is TurnStatus.HARNESS_ERROR
    assert out.error is not None and "model was not found" in out.error


def test_openclaw_envelope_without_transcript_is_error() -> None:
    envelope = (FIXTURES / "openclaw" / "turn1.stdout").read_text().split("\n--- openclaw trajectory events ---\n")[0]
    out = PARSERS[ParserId.OPENCLAW_JSON](envelope, "")
    assert out.status is TurnStatus.HARNESS_ERROR
    assert out.session_id == "9fc5fff5-fff8-42a8-8a80-f91e6581816b"


def test_openclaw_transcript_without_this_run_is_error() -> None:
    # An export holding only earlier runs must not read as a silent, empty completed turn.
    lines = (FIXTURES / "openclaw" / "resume.stdout").read_text().splitlines(keepends=True)
    run_id = "1dc0e870-a9e7-4639-80ac-af1d9a364f35"
    stale = "".join(line for line in lines if run_id not in line or line.startswith(" "))
    out = PARSERS[ParserId.OPENCLAW_JSON](stale, "")
    assert out.status is TurnStatus.HARNESS_ERROR
    assert out.error is not None and "no assistant message" in out.error


def test_openclaw_truncated_run_stamp_is_recovered_from_its_tool_results() -> None:
    # The export truncates `__openclaw` on long tool-calling messages; the tool results after keep it.
    raw = (FIXTURES / "openclaw" / "turn1.stdout").read_text()
    head, events = raw.split("\n--- openclaw trajectory events ---\n")
    lines = events.splitlines(keepends=True)
    first = next(i for i, ln in enumerate(lines) if '"assistant.message"' in ln and '"toolCall"' in ln)
    event = json.loads(lines[first])
    event["data"]["message"]["__openclaw"] = "[Truncated]"
    event["data"]["message"]["content"].append("[Truncated]")
    lines[first] = json.dumps(event) + "\n"
    out = PARSERS[ParserId.OPENCLAW_JSON](head + "\n--- openclaw trajectory events ---\n" + "".join(lines), "")
    full = parse(ParserId.OPENCLAW_JSON, "openclaw", 1)
    assert (out.status, out.answer, tool_calls(out)) == (full.status, full.answer, tool_calls(full))
    assert [s.output for s in out.steps if s.name == "openclaw:truncated"] == ["1 content blocks truncated"]
    orphan = PARSERS[ParserId.OPENCLAW_JSON](head + "\n--- openclaw trajectory events ---\n" + lines[first], "")
    assert orphan.status is TurnStatus.HARNESS_ERROR


@pytest.mark.parametrize("pid", list(ParserId))
def test_malformed_output_is_harness_error_not_crash(pid: ParserId) -> None:
    out = PARSERS[pid]("{not json\n", "")
    if pid is ParserId.PLAIN_TEXT:
        assert out.status is TurnStatus.COMPLETED
        return
    assert out.status is TurnStatus.HARNESS_ERROR
    assert out.error


def test_plain_text_empty_is_error() -> None:
    assert PARSERS[ParserId.PLAIN_TEXT]("  \n", "").status is TurnStatus.HARNESS_ERROR
