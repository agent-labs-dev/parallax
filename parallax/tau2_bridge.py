"""Small process boundary for the pinned upstream tau2-bench evaluator.

Run as a script by the pinned tau2 checkout's own Python, like `official_bridge.py`. It imports that
checkout and calls upstream `evaluate_simulation` with `EvaluationType.ALL`, upstream's default
SEMANTIC communicate scoring and its NL-assertion evaluator (both with upstream's pinned judge
models); no database, action or communication comparison is reimplemented here. The harness reports
`{transcript, termination_reason}` as trial evidence: the full upstream message transcript, whose
assistant tool calls are each followed by their ToolMessage, and an explicit termination reason.
The evaluator replays the transcript itself. Simulator (user-model) usage never enters this bridge:
the simulation is built with no agent/user cost, and every judge call the evaluator makes is
recorded as it happens - in `calls`, as a `PARALLAX_JUDGE_CALL` JSON line on stderr, and in the
request's `calls_file` when one is given - priced when upstream reported a cost and unpriced
otherwise, including a call that fails. Termination reasons come from `tau2_terms.py`, the
single source this bridge and `parallax.official_suites.Tau2Evidence` both validate against.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _load_terms() -> tuple[str, ...]:
    """The shared termination reasons from `tau2_terms.py` beside this file, loaded by path:
    this bridge runs under the pinned checkout's Python, where parallax is not installed."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("tau2_terms", Path(__file__).with_name("tau2_terms.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("tau2_terms.py cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.TERMINATION_REASONS)


TERMINATION_REASONS = _load_terms()
_TERMINATION_SET = frozenset(TERMINATION_REASONS)
JUDGE_CALL_PREFIX = "PARALLAX_JUDGE_CALL "


def _root(request: dict[str, Any]) -> Path:
    root = Path(str(request["root"])).resolve()
    if not root.is_dir():
        raise RuntimeError("upstream checkout is not a directory")
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    if head != request["commit"]:
        raise RuntimeError("upstream checkout is not at the pinned revision")
    status = subprocess.run(
        ("git", "status", "--porcelain"), cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    if status:
        raise RuntimeError("upstream checkout has tracked changes")
    here = Path(__file__).resolve().parent
    front = [str(root / "src"), str(root)]
    sys.path[:] = [*front, *(e for e in sys.path if e not in front and Path(e or ".").resolve() != here)]
    return root


def _messages(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Evidence messages as plain dicts, validated against the upstream transcript contract."""
    if request.get("tool_calls"):
        raise RuntimeError("bare tool_calls evidence is not accepted; tool calls belong in the transcript")
    if "termination_reason" not in request:
        raise RuntimeError("termination_reason is required")
    if request["termination_reason"] not in _TERMINATION_SET:
        raise RuntimeError(f"unknown termination_reason {request['termination_reason']!r}")
    transcript = request.get("transcript")
    if not isinstance(transcript, list) or not transcript:
        raise RuntimeError("transcript must be a non-empty list of tau2 messages")
    messages: list[dict[str, Any]] = []
    for entry in transcript:
        if not isinstance(entry, dict) or entry.get("role") not in {"system", "user", "assistant", "tool"}:
            raise RuntimeError("transcript entries must be tau2 messages with a role")
        for call in entry.get("tool_calls") or []:
            if not isinstance(call, dict) or not call.get("name"):
                raise RuntimeError("tool_calls entries must name the tool called")
            if not isinstance(call.get("arguments", {}), dict):
                raise RuntimeError("tool call arguments must be an object, not a string")
        messages.append(dict(entry))
    for index, message in enumerate(messages):
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            continue
        following = messages[index + 1 : index + 1 + len(calls)]
        if len(following) != len(calls) or any(m.get("role") != "tool" for m in following):
            raise RuntimeError("assistant tool calls must each be followed by their ToolMessage, in order")
        for call, tool_message in zip(calls, following, strict=True):
            if call.get("id") and tool_message.get("id") not in (None, call["id"]):
                raise RuntimeError("ToolMessage id does not match its tool call")
    return messages


def _emit_call(calls: list[dict[str, Any]], calls_file: str | None, entry: dict[str, Any]) -> None:
    calls.append(entry)
    print(JUDGE_CALL_PREFIX + json.dumps(entry), file=sys.stderr, flush=True)
    if calls_file:
        with Path(calls_file).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")


def _judge_calls(calls: list[dict[str, Any]], calls_file: str | None = None) -> Any:
    """Wrap the evaluator modules' `generate` so every judge call is recorded as it happens.

    Returns a restore callable. Each entry carries the model called, token usage when the reply
    reported any, and its cost (None, never dropped, when upstream reported none); a call that
    raises is recorded unpriced with its error and then re-raised.
    """
    import importlib

    patched: list[tuple[Any, Any]] = []
    for module_name in ("tau2.evaluator.evaluator_communicate", "tau2.evaluator.evaluator_nl_assertions"):
        module = importlib.import_module(module_name)
        original = module.generate

        def recording(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            started = time.monotonic()
            model = str(kwargs.get("model") or "unknown")
            try:
                reply = _original(*args, **kwargs)
            except BaseException as exc:
                _emit_call(calls, calls_file, {
                    "model": model, "input_tokens": None, "output_tokens": None, "cost_usd": None,
                    "latency_ms": int((time.monotonic() - started) * 1000), "error": f"{type(exc).__name__}: {exc}",
                })  # fmt: skip
                raise
            usage = getattr(reply, "usage", None) or {}
            _emit_call(calls, calls_file, {
                "model": str(kwargs.get("model") or getattr(reply, "model", "") or "unknown"),
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "cost_usd": getattr(reply, "cost", None),
                "latency_ms": int((time.monotonic() - started) * 1000),
            })  # fmt: skip
            return reply

        patched.append((module, original))
        module.__dict__["generate"] = recording

    def restore() -> None:
        for module, original in patched:
            module.__dict__["generate"] = original

    return restore


def _evaluate(request: dict[str, Any]) -> dict[str, Any]:
    import importlib

    evaluator = importlib.import_module("tau2.evaluator.evaluator")
    helpers = importlib.import_module("tau2.runner.helpers")
    message_module = importlib.import_module("tau2.data_model.message")
    simulation_module = importlib.import_module("tau2.data_model.simulation")
    scoring_module = importlib.import_module("tau2.data_model.semantic_communication")

    domain = str(request["domain"])
    task_id = str(request["task_id"])
    tasks = helpers.get_tasks(domain, task_ids=[task_id])
    if len(tasks) != 1:
        raise RuntimeError(f"upstream tau2 task {domain}/{task_id} not found")
    task = tasks[0]

    by_role = {
        "system": message_module.SystemMessage,
        "user": message_module.UserMessage,
        "assistant": message_module.AssistantMessage,
        "tool": message_module.ToolMessage,
    }
    messages = [by_role[m["role"]].model_validate(m) for m in _messages(request)]
    simulation = simulation_module.SimulationRun(
        id=str(request.get("simulation_id") or f"parallax-{domain}-{task_id}"),
        task_id=task_id,
        start_time=str(request.get("start_time") or "1970-01-01T00:00:00"),
        end_time=str(request.get("end_time") or "1970-01-01T00:00:00"),
        duration=float(request.get("duration") or 0),
        termination_reason=simulation_module.TerminationReason(str(request["termination_reason"])),
        messages=messages,
        agent_cost=None,  # simulator and agent cost stay out of the upstream reward path
        user_cost=None,
    )
    calls: list[dict[str, Any]] = []
    restore = _judge_calls(calls, str(request["calls_file"]) if request.get("calls_file") else None)
    # Route both judges through the caller's configured gateway with the ids it validated:
    # the communicate judge via its settings, the NL judge via the module's pinned name.
    judge_models = request.get("judge_models") or {}
    communicate_model = str(judge_models.get("communicate") or "")
    nl_model = str(judge_models.get("nl") or "")
    nl_module = importlib.import_module("tau2.evaluator.evaluator_nl_assertions")
    pinned_nl = getattr(nl_module, "DEFAULT_LLM_NL_ASSERTIONS", None)
    if nl_model:
        nl_module.__dict__["DEFAULT_LLM_NL_ASSERTIONS"] = nl_model
    try:
        reward = evaluator.evaluate_simulation(
            simulation=simulation,
            task=task,
            evaluation_type=evaluator.EvaluationType.ALL,
            solo_mode=False,
            domain=domain,
            communicate_scoring=(
                scoring_module.CommunicateScoring(
                    judge=scoring_module.SemanticCommunicateSettings(model=communicate_model)
                )
                if communicate_model
                else scoring_module.CommunicateScoring()
            ),
        )
    finally:
        restore()
        if pinned_nl is not None:
            nl_module.__dict__["DEFAULT_LLM_NL_ASSERTIONS"] = pinned_nl
    score = float(reward.reward)
    return {
        "passed": score == 1.0,
        "score": score,
        "reward": score,
        "reason": json.dumps(reward.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
        "calls": calls,
    }


def failure(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())


def main() -> None:
    request = json.load(sys.stdin)
    if not isinstance(request, dict):
        raise RuntimeError("bridge request must be an object")
    _root(request)
    if request.get("operation") != "evaluate" or request.get("suite") != "tau2":
        raise RuntimeError("unsupported bridge operation")
    with contextlib.redirect_stdout(sys.stderr):
        result = _evaluate(request)
    json.dump({**result, "calls": result.get("calls", [])}, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print(failure(exc), file=sys.stderr)
        raise SystemExit(1) from None
