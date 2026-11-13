"""Pure stdout/stderr -> TurnOutput parsers for manifest CLI harnesses.

Each parser turns one CLI invocation's output into a `TurnOutput` whose status is COMPLETED, or
HARNESS_ERROR when the output itself reports a failure or cannot be read. Timeouts and exit codes
are the caller's concern (`SubprocessHarness`), since a parser only sees text.

Conventions shared by every parser:
- `Step.turn` is 0 and `Step.t_ms` is 0: batch CLI output carries no turn index and no per-event
  clock. `SubprocessHarness` stamps the real turn index.
- `Usage.model` is "" when the CLI does not report which model served the turn;
  `SubprocessHarness` stamps the requested model. Usage is always `CostSource.HARNESS` here:
  the meter's own usage replaces it downstream when a row is metered.
- Tool names are canonicalised (`mcp__world__mail_send` -> `mail_send`) so diagnostics read the
  same across harnesses.
- `TurnOutput.answer` is all assistant text of the turn, in order, joined by blank lines
  (`turn_answer`). hermes quiet mode exposes only the closing message, so for it the answer is
  that message alone, and its steps come from the session export the manifest prints after it.

Every format below was checked against real output of the installed CLI, except where the
fixture is flagged `synthetic` (see tests/harnesses/fixtures/*/README.md).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from typing import assert_never

from pydantic import JsonValue

from parallax.models import CostSource, JsonObject, ParserId, Step, StepKind, TurnOutput, TurnStatus, Usage

Parser = Callable[[str, str], TurnOutput]

# Harnesses namespace MCP tools as <prefix><server><sep><tool>; the world server is always "world".
_MCP_PREFIX = re.compile(r"^(?:mcp__world__|mcp_world_|world__|world\.|world/)")


class ParseError(ValueError):
    """Output that does not have the shape its parser documents."""


def canonical_tool_name(name: str) -> str:
    return _MCP_PREFIX.sub("", name)


def json_lines(stdout: str) -> Iterator[JsonObject]:
    """Every non-blank stdout line as a JSON object; anything else is a ParseError, never skipped."""
    for n, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ParseError(f"stdout line {n} is not JSON: {line[:200]!r}") from exc
        if not isinstance(obj, dict):
            raise ParseError(f"stdout line {n} is not a JSON object")
        yield obj


def obj(value: JsonValue, where: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ParseError(f"{where}: expected an object, got {type(value).__name__}")
    return value


def objs(value: JsonValue, where: str) -> list[JsonObject]:
    if not isinstance(value, list):
        raise ParseError(f"{where}: expected a list, got {type(value).__name__}")
    return [obj(v, where) for v in value]


def text(value: JsonValue, where: str) -> str:
    if not isinstance(value, str):
        raise ParseError(f"{where}: expected a string, got {type(value).__name__}")
    return value


def opt_text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def count(value: JsonValue) -> int:
    """Token counters: absent or null means the CLI did not report it, which is 0 tokens counted."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParseError(f"token count is not an integer: {value!r}")
    return value


def step(
    kind: StepKind,
    *,
    name: str | None = None,
    args: JsonValue = None,
    output: str | None = None,
    ok: bool | None = None,
) -> Step:
    return Step(turn=0, kind=kind, name=name, args=args, output=output, ok=ok, t_ms=0)


def failed(error: str, *, answer: str = "", session_id: str | None = None, steps: tuple[Step, ...] = ()) -> TurnOutput:
    return TurnOutput(status=TurnStatus.HARNESS_ERROR, answer=answer, session_id=session_id, steps=steps, error=error)


def guarded(pid: ParserId, parser: Parser) -> Parser:
    """A malformed output is a harness error for that turn, reported with the reason, not a crash."""

    def run(stdout: str, stderr: str) -> TurnOutput:
        try:
            return parser(stdout, stderr)
        except ParseError as exc:
            return failed(f"{pid}: {exc}")

    return run


def turn_answer(texts: Iterable[str]) -> str:
    """A turn's answer is every piece of assistant text it wrote, in order: text written before or
    between tool calls (a summary, then the action) is as much the reply as the closing line."""
    return "\n\n".join(t for t in (t.strip() for t in texts) if t)


def result_text(content: JsonValue) -> str:
    """MCP/Anthropic tool_result content: a string or a list of blocks; text blocks are joined."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return "\n".join(
        text(b.get("text"), "tool_result text") for b in objs(content, "tool_result") if b.get("type") == "text"
    )


# ---------------------------------------------------------------------------
# openclaw: `openclaw agent --local --json`, then the session's trajectory transcript
# ---------------------------------------------------------------------------

# The manifest prints this line between the `agent --json` envelope and the transcript events.
OPENCLAW_EVENTS_MARKER = "--- openclaw trajectory events ---"


def openclaw_json(stdout: str, stderr: str) -> TurnOutput:
    """The `agent --json` envelope `{payloads, meta}`, the marker line, then the transcript events of
    `openclaw sessions export-trajectory` as JSON lines (openclaw 2026.9.2).

    The envelope's `payloads` carry only the closing message, so text, tool calls and tool results
    come from the transcript's `assistant.message` / `tool.result` events, keeping those stamped with
    this turn's run id (`meta.agentMeta.terminalReceipt.runId`): the export covers the whole session.
    """
    head, marker, events = stdout.partition(f"\n{OPENCLAW_EVENTS_MARKER}\n")
    try:
        env = obj(json.loads(head), "openclaw envelope")
    except json.JSONDecodeError as exc:
        raise ParseError(f"openclaw envelope is not one JSON document: {exc}") from exc
    if env.get("ok") is False:
        error = obj(env.get("error"), "openclaw error")
        return failed(f"openclaw {error.get('type')}: {error.get('message')}")
    meta = obj(env.get("meta"), "openclaw meta")
    agent = obj(meta.get("agentMeta"), "meta.agentMeta")
    session_id = text(agent.get("sessionId"), "agentMeta.sessionId")
    if not marker:
        return failed("openclaw: no trajectory export after the envelope (see stderr)", session_id=session_id)
    receipt = obj(agent.get("terminalReceipt"), "agentMeta.terminalReceipt")
    run_id = text(receipt.get("runId"), "terminalReceipt.runId")
    steps, texts = openclaw_transcript(json_lines(events), run_id)
    effective = obj(receipt.get("effective"), "terminalReceipt.effective")
    # responseModel is what the provider says answered; the requested model only labels the usage row.
    actual = opt_text(effective.get("responseModel"))
    requested = text(agent.get("model"), "agentMeta.model")
    error = None
    if meta.get("aborted") is True or meta.get("stopReason") == "error":
        shown = [opt_text(p.get("text")) for p in objs(env.get("payloads"), "payloads") if p.get("isError") is True]
        error = f"openclaw stopReason {meta.get('stopReason')}: {'; '.join(t for t in shown if t) or 'aborted'}"
    elif not any(s.kind in (StepKind.MESSAGE, StepKind.TOOL_CALL) for s in steps):
        # A finished run always records an assistant message; none under this run id means the export's
        # layout or run stamping changed, and an empty COMPLETED turn would grade as the model's silence.
        error = f"openclaw: transcript has no assistant message for run {run_id}"
    return TurnOutput(
        status=TurnStatus.HARNESS_ERROR if error else TurnStatus.COMPLETED,
        answer=turn_answer(texts),
        session_id=session_id,
        steps=tuple(steps),
        usage=openclaw_usage(agent, actual or requested),
        actual_model=actual,
        error=error,
    )


# The export replaces what is past its size limits with this marker: a long tool-calling message
# loses its `__openclaw.runId` and its later content blocks, while its tool results keep their runId.
OPENCLAW_TRUNCATED = "[Truncated]"


def openclaw_messages(events: Iterable[JsonObject]) -> list[tuple[JsonObject, str]]:
    """Each assistant message and tool result with its run id. A message whose stamp was truncated
    takes the run of the nearest stamped message after the same user prompt, the following one first."""
    prompts: list[list[tuple[JsonObject, str | None]]] = [[]]
    for ev in events:
        kind = ev.get("type")
        if kind == "user.message":
            prompts.append([])
        if kind not in ("assistant.message", "tool.result"):
            continue
        message = obj(obj(ev.get("data"), f"{kind}.data").get("message"), f"{kind}.message")
        stamp = message.get("__openclaw")
        if stamp == OPENCLAW_TRUNCATED:
            prompts[-1].append((message, None))
        elif (run := opt_text(obj(stamp, "message.__openclaw").get("runId"))) is not None:
            prompts[-1].append((message, run))
    out: list[tuple[JsonObject, str]] = []
    for group in prompts:
        for i, (message, run) in enumerate(group):
            nearest = [r for _, r in (*group[i + 1 :], *reversed(group[:i])) if r is not None]
            if run is None and not nearest:
                raise ParseError("openclaw message lost its run stamp and no message after its prompt has one")
            out.append((message, run if run is not None else nearest[0]))
    return out


def openclaw_transcript(events: Iterable[JsonObject], run_id: str) -> tuple[list[Step], list[str]]:
    steps: list[Step] = []
    texts: list[str] = []
    for message, run in openclaw_messages(events):
        if run != run_id:
            continue
        if message.get("role") == "toolResult":
            steps.append(
                step(
                    StepKind.TOOL_RESULT,
                    name=canonical_tool_name(text(message.get("toolName"), "toolResult.toolName")),
                    output=result_text(message.get("content")),
                    ok=message.get("isError") is not True,
                )
            )
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else objs(content, "assistant.content")
        # Content blocks past the export's size limit arrive as the marker; the world log still holds
        # every call they made, so record how many were lost rather than dropping them unseen.
        lost = sum(block == OPENCLAW_TRUNCATED for block in blocks)
        if lost:
            steps.append(step(StepKind.MESSAGE, name="openclaw:truncated", output=f"{lost} content blocks truncated"))
        for block in (obj(b, "assistant.content") for b in blocks if b != OPENCLAW_TRUNCATED):
            match block.get("type"):
                case "text":
                    body = text(block.get("text"), "assistant text")
                    texts.append(body)
                    steps.append(step(StepKind.MESSAGE, output=body))
                case "toolCall":
                    name = canonical_tool_name(text(block.get("name"), "toolCall.name"))
                    steps.append(step(StepKind.TOOL_CALL, name=name, args=block.get("arguments")))
                case "thinking":
                    # Reasoning is not a diagnostic step.
                    continue
                case other:
                    raise ParseError(f"openclaw assistant content block of unknown type {other!r}")
    return steps, texts


def openclaw_usage(agent: JsonObject, model: str) -> tuple[Usage, ...]:
    raw = agent.get("usage")
    if raw is None:
        return ()
    u = obj(raw, "agentMeta.usage")
    return (
        Usage(
            model=model,
            input=count(u.get("input")),
            output=count(u.get("output")),
            cache_read=count(u.get("cacheRead")),
            cache_write=count(u.get("cacheWrite")),
            reasoning=count(u.get("reasoningTokens")),
            requests=count(agent.get("assistantTurns")),
            # The manifest's meter providers declare no prices, so openclaw's `costUsd` of 0 means
            # "unpriced", not free; the meter's own cost replaces this row downstream.
            cost_usd=None,
            source=CostSource.HARNESS,
        ),
    )


# ---------------------------------------------------------------------------
# hermes: `hermes chat -Q -q ...`
# ---------------------------------------------------------------------------

_HERMES_SESSION = re.compile(r"^session_id: (\S+)$", re.MULTILINE)
_HERMES_ERROR = re.compile(r"^Error: (.+)$", re.MULTILINE)


HERMES_EXPORT = "--- hermes session export ---"
"""The manifest prints this line, then `hermes sessions export - --session-id <id>`, after the answer."""


def hermes_quiet(stdout: str, stderr: str) -> TurnOutput:
    """Quiet mode prints only the final response on stdout; `session_id: <id>` and any
    `Error: <msg>` go to stderr (hermes 0.19 cli.py single-query path). The tool steps are the
    session export's messages after this turn's prompt: without them a judge reads "Saved." with no
    memory call behind it as an unbacked claim (#34)."""
    answer, marker, export = stdout.partition(HERMES_EXPORT + "\n")
    answer = answer.strip()
    sessions = _HERMES_SESSION.findall(stderr)
    session_id = sessions[-1] if sessions else None
    errors = _HERMES_ERROR.findall(stderr)
    error = None
    if errors:
        error = f"hermes: {errors[-1]}"
    elif session_id is None:
        error = "hermes: no session_id trailer on stderr, so the conversation cannot continue"
    elif not answer:
        error = "hermes: empty response"
    tools: tuple[Step, ...] = ()
    if session_id is not None and not marker:
        error = error or "hermes: no session export after the answer, so its tool steps are unknown"
    elif marker:
        tools = tuple(_hermes_tool_steps(export))
    steps = (*tools, step(StepKind.MESSAGE, output=answer)) if answer else tools
    return TurnOutput(
        status=TurnStatus.HARNESS_ERROR if error else TurnStatus.COMPLETED,
        answer=answer,
        session_id=session_id,
        steps=steps,
        error=error,
    )


def _hermes_tool_steps(export: str) -> Iterator[Step]:
    """Each tool call after the session's last user message, with the result its `tool` row returned."""
    sessions = list(json_lines(export))
    if len(sessions) != 1 or not isinstance(messages := sessions[0].get("messages"), list):
        raise ParseError("hermes session export is not one session with messages")
    rows = [m for m in messages if isinstance(m, dict)]
    last_user = max((i for i, m in enumerate(rows) if m.get("role") == "user"), default=-1)
    turn = rows[last_user + 1 :]
    results = {m.get("tool_call_id"): m.get("content") for m in turn if m.get("role") == "tool"}
    for message in turn:
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        for call in calls if isinstance(calls, list) else ():
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or not isinstance(name := function.get("name"), str):
                raise ParseError(f"hermes tool call without a function name: {str(call)[:200]}")
            raw = function.get("arguments")
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = raw
            output = results.get(call.get("id"))
            yield step(
                StepKind.TOOL_CALL,
                name=canonical_tool_name(name),
                args=args,
                output=output if isinstance(output, str) else None if output is None else json.dumps(output),
            )


# ---------------------------------------------------------------------------
# plain text
# ---------------------------------------------------------------------------


def plain_text(stdout: str, stderr: str) -> TurnOutput:
    """The whole stdout is the answer; no session, so such a harness cannot declare MULTI_TURN."""
    answer = stdout.strip()
    if not answer:
        return failed("plain_text: empty stdout")
    return TurnOutput(
        status=TurnStatus.COMPLETED, answer=answer, session_id=None, steps=(step(StepKind.MESSAGE, output=answer),)
    )


def parser_for(pid: ParserId) -> Parser:
    match pid:
        case ParserId.OPENCLAW_JSON:
            return openclaw_json
        case ParserId.HERMES_QUIET:
            return hermes_quiet
        case ParserId.PLAIN_TEXT:
            return plain_text
        case _:
            assert_never(pid)


PARSERS: dict[ParserId, Parser] = {pid: guarded(pid, parser_for(pid)) for pid in ParserId}
