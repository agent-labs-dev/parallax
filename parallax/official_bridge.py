"""Small process boundary for the pinned upstream browser judges.

This file contains no scoring prompt. It imports the selected checkout and calls the public judge
functions exposed by that revision, which keeps the upstream judge authoritative and replaceable only
by changing the recorded revision.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import json
import os
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
    for entry in (root, root / "src"):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    return root


def _check_bu_dataset(root: Path, expected: str) -> None:
    data = (root / "BU_Bench_V2.enc").read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError("upstream checkout contains a different encrypted dataset")


def _complete(response: Any) -> bool:
    choices = getattr(response, "choices", ())
    return bool(choices) and all(
        getattr(choice, "finish_reason", None) in (None, "stop") and getattr(choice.message, "content", None)
        for choice in choices
    )


def _labelled_images(messages: Any) -> Any:
    value = json.dumps(messages, default=lambda item: item.model_dump(mode="json"))
    return json.loads(value.replace("data:image/png;base64,/9j/", "data:image/jpeg;base64,/9j/"))


def _judge_bu(request: dict[str, Any]) -> dict[str, Any]:
    evaluation = importlib.import_module("evaluation")

    root = Path(request["root"])
    _check_bu_dataset(root, str(request["dataset_sha256"]))
    task = next(task for task in evaluation.load_tasks("BU_Bench_V2") if task["task_id"] == request["task_id"])
    judge = evaluation.create_judge(
        "BU_Bench_V2", model=request["judge_model"], reasoning_effort=request.get("reasoning", "xhigh")
    )
    result = asyncio.run(evaluation.judge_trace(task, request["trace"], judge, "BU_Bench_V2"))
    return {
        "passed": bool(result.get("verdict")),
        "score": float(result.get("score", result.get("raw_score"))),
        "judge_model": request["judge_model"],
    }


def _judge_mind2web(request: dict[str, Any]) -> dict[str, Any]:
    webjudge = importlib.import_module("methods.webjudge_online_mind2web")
    openai = importlib.import_module("openai")
    utils = importlib.import_module("utils")
    WebJudge_Online_Mind2Web_eval = webjudge.WebJudge_Online_Mind2Web_eval
    OpenAI = openai.OpenAI
    OpenaiEngine, extract_predication = utils.OpenaiEngine, utils.extract_predication

    model = OpenaiEngine(model=request["judge_model"], api_key=os.environ["OPENAI_API_KEY"])
    model.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ.get("OPENAI_BASE_URL"))
    original_generate = model.generate
    reasoning_model = str(request["judge_model"]).lower().startswith(("o1", "o3", "o4", "gpt-5"))

    def generate(messages: Any, *args: Any, **kwargs: Any) -> Any:
        labelled = _labelled_images(messages)
        if not reasoning_model:
            return original_generate(labelled, *args, **kwargs)
        response = model.client.chat.completions.create(
            model=request["judge_model"],
            messages=labelled,
            max_completion_tokens=32768,
            reasoning_effort=request.get("reasoning", "xhigh"),
        )
        if not _complete(response):
            raise RuntimeError("upstream judge returned no complete response")
        return [choice.message.content for choice in response.choices]

    model.generate = generate
    artifact = Path(str(request["artifact"]))
    artifact.mkdir(parents=True, exist_ok=True)
    screenshots: list[str] = []
    for index, encoded in enumerate(request.get("screenshots", [])):
        if not isinstance(encoded, str):
            raise RuntimeError("screenshot evidence is not base64 text")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("screenshot evidence is not valid base64") from exc
        path = artifact / f"screenshot-{index:04d}.png"
        path.write_bytes(data)
        screenshots.append(str(path))
    _messages, _text, _system, _record, _points = asyncio.run(
        WebJudge_Online_Mind2Web_eval(
            request["task"], request["actions"], screenshots, model, int(request["score_threshold"])
        )
    )
    response = model.generate(_messages)[0]
    label = extract_predication(response, "WebJudge_Online_Mind2Web_eval")
    if label not in (0, 1):
        raise RuntimeError("upstream judge did not return a binary result")
    return {"passed": bool(label), "score": float(label), "judge_model": request["judge_model"]}


def main() -> None:
    request = json.load(sys.stdin)
    if not isinstance(request, dict):
        raise RuntimeError("bridge request must be an object")
    _root(request)
    suite = request.get("suite")
    if request.get("operation") != "judge":
        raise RuntimeError("unsupported bridge operation")
    result = _judge_bu(request) if suite == "bu-bench" else _judge_mind2web(request)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print(type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
