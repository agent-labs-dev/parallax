"""Small process boundary for the pinned upstream tau2-bench evaluator.

Run as a script by the pinned tau2 checkout's own Python, like `official_bridge.py`. It imports that
checkout and calls upstream `evaluate_simulation` with `EvaluationType.ALL`; no database, action or
communication comparison is reimplemented here. The harness reports
`{transcript, tool_calls, final_db_state}` as trial evidence; the transcript is authoritative, and a
bare `tool_calls` list is only used to synthesise assistant tool-call messages when the transcript
carries none. Simulator (user-model) usage never enters this bridge's result as agent cost: the
simulation is built with no agent/user cost, and any simulator calls the harness made stay in trial
metadata, outside `TurnOutput.usage`.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


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
    """Evidence messages as plain dicts, in tau2 message shape, synthesising tool calls if needed."""
    transcript = request.get("transcript") or []
    messages: list[dict[str, Any]] = []
    for entry in transcript:
        if not isinstance(entry, dict) or entry.get("role") not in {"system", "user", "assistant", "tool"}:
            raise RuntimeError("transcript entries must be tau2 messages with a role")
        messages.append(dict(entry))
    tool_calls = request.get("tool_calls") or []
    already = any(isinstance(m.get("tool_calls"), list) and m["tool_calls"] for m in messages)
    if tool_calls and not already:
        for call in tool_calls:
            if not isinstance(call, dict) or not call.get("name"):
                raise RuntimeError("tool_calls entries must name the tool called")
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(call.get("id") or ""),
                            "name": str(call["name"]),
                            "arguments": dict(call.get("arguments") or {}),
                            "requestor": "assistant",
                        }
                    ],
                }
            )
    return messages


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
        termination_reason=str(request.get("termination_reason") or "agent_stop"),
        messages=messages,
        agent_cost=None,  # simulator and agent cost stay out of the upstream reward path
        user_cost=None,
        info={"final_db_state": request.get("final_db_state")},
    )
    # Substring communicate scoring keeps the bridge free of a second LLM judge call; upstream still
    # decides ENV, ACTION and (when in the task's reward basis) COMMUNICATE under EvaluationType.ALL.
    reward = evaluator.evaluate_simulation(
        simulation=simulation,
        task=task,
        evaluation_type=evaluator.EvaluationType.ALL,
        solo_mode=False,
        domain=domain,
        communicate_scoring=scoring_module.CommunicateScoring(scorer=scoring_module.CommunicateScorer.SUBSTRING),
    )
    score = float(reward.reward)
    return {
        "passed": score == 1.0,
        "score": score,
        "reward": score,
        "reason": json.dumps(reward.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
        "calls": [],
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
