"""Small process boundary for the pinned upstream browser judges.

This file contains no scoring prompt. It imports the selected checkout and calls the public judge
functions exposed by that revision, which keeps the upstream judge authoritative and replaceable only
by changing the recorded revision.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
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
    sys.path[:] = _import_path(root, sys.path)
    return root


def _import_path(root: Path, path: list[str]) -> list[str]:
    """The checkout first, and never this file's own directory. Run as a script, the bridge has the `parallax`
    package first on `sys.path`, where its `judge` and `models` shadow the upstream modules of those names: BU
    Bench's `import judge` loaded Parallax's and failed with `No module named 'parallax'`. An editable install of
    the upstream project already lists the root, but after that directory, so the root moves to the front."""
    here = Path(__file__).resolve().parent
    front = [str(root / "src"), str(root)]
    return [*front, *(entry for entry in path if entry not in front and Path(entry or ".").resolve() != here)]


def _call(response: Any, started: float) -> dict[str, Any]:
    """One judge call's measured usage. The cost is the endpoint's own `usage.cost` (OpenRouter reports it on every
    completion); an endpoint that reports none leaves it None, which Parallax counts as unpriced, never as zero."""
    usage = getattr(response, "usage", None)
    cost = (getattr(usage, "model_extra", None) or {}).get("cost")
    return {
        "generation_id": getattr(response, "id", None),
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def _unpriced(error: str) -> dict[str, Any]:
    """An attempt that may have been billed but returned no usage: Parallax counts it as unpriced, never as zero."""
    return {"generation_id": None, "input_tokens": None, "output_tokens": None, "cost_usd": None, "latency_ms": 0,
            "error": error}  # fmt: skip


def _meter(calls: list[dict[str, Any]]) -> None:
    """Record every chat completion the upstream judge makes, whichever client it builds, by wrapping the SDK's own
    `create` in this process; the judge's prompts and parsing are untouched. The SDK retries inside `create`, and a
    retried attempt may already have been billed, so each retry, and each `create` that raises, records an unpriced
    attempt and the trial keeps its budget hold."""
    completions = importlib.import_module("openai.resources.chat.completions")
    client = importlib.import_module("openai._base_client")
    sync_create, async_create = completions.Completions.create, completions.AsyncCompletions.create
    # Every HTTP attempt, sync or async, is built by `BaseClient._build_request(options, retries_taken=n)`, which also
    # sends `n` as the `x-stainless-retry-count` header; n > 0 is a retry. The upstream judges pin different majors
    # (Online-Mind2Web 1.68.2, BU Bench 2.15.0), and the retry loop differs in each, but this call is the same in 1.x,
    # 2.x and 3.x.
    build_request = client.BaseClient._build_request

    def create(self: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            response = sync_create(self, *args, **kwargs)
        except BaseException as exc:
            calls.append(_unpriced(type(exc).__name__))
            raise
        calls.append(_call(response, started))
        return response

    async def acreate(self: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            response = await async_create(self, *args, **kwargs)
        except BaseException as exc:
            calls.append(_unpriced(type(exc).__name__))
            raise
        calls.append(_call(response, started))
        return response

    def build(self: Any, options: Any, *, retries_taken: int = 0) -> Any:
        request = build_request(self, options, retries_taken=retries_taken)
        if retries_taken:
            calls.append(_unpriced("retried"))
        return request

    completions.Completions.create = create
    completions.AsyncCompletions.create = acreate
    client.BaseClient._build_request = build


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
        # The rubric findings the score was computed from.
        "reason": json.dumps(result.get("judgement"), ensure_ascii=False, sort_keys=True),
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

    # Upstream's `generate` sends `max_tokens=512`, which a reasoning judge spends before it writes the "Status:" line:
    # every DeepSeek V4.1 Flash reply that failed a 10-task smoke stopped at 512 tokens with no verdict. Every model
    # goes through this call instead, with the reply budget and pinned sampling fields Parallax's own judge sends that
    # model (`parallax.judging`, which the upstream environment cannot import), and a reply cut short raises, so the
    # trial is invalid rather than failed.
    def generate(messages: Any, *args: Any, **kwargs: Any) -> Any:
        response = model.client.chat.completions.create(
            model=request["judge_model"],
            messages=_labelled_images(messages),
            max_tokens=request["max_tokens"],
            extra_body=request["sampling"],
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
    return {"passed": bool(label), "score": float(label), "judge_model": request["judge_model"], "reason": response}


def failure(exc: BaseException, secret: str) -> str:
    """One line naming why the bridge failed, for the trial's invalid reason; the judge's key never appears in it."""
    line = " ".join(f"{type(exc).__name__}: {exc}".split())
    return line.replace(secret, "[redacted]") if secret else line


def main() -> None:
    request = json.load(sys.stdin)
    if not isinstance(request, dict):
        raise RuntimeError("bridge request must be an object")
    _root(request)
    suite = request.get("suite")
    if request.get("operation") != "judge":
        raise RuntimeError("unsupported bridge operation")
    calls: list[dict[str, Any]] = []
    _meter(calls)
    # stdout carries only the bridge's JSON result. The upstream judges print their own diagnostics to stdout (the
    # Online-Mind2Web WebJudge prints "Error processing response" when a snapshot reply holds no score), so they
    # write to stderr while they run.
    with contextlib.redirect_stdout(sys.stderr):
        result = _judge_bu(request) if suite == "bu-bench" else _judge_mind2web(request)
    json.dump({**result, "calls": calls}, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print(failure(exc, os.environ.get("OPENAI_API_KEY", "")), file=sys.stderr)
        raise SystemExit(1) from None
