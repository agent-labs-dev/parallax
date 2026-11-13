"""A judge endpoint that replays recorded Jev and fallback replies, keyed by the exact request body.

The world runs on a fixed clock, so a control's evidence, and therefore its judge request, is identical on
every run. Any change to evidence, questions, prompt or model misses the recording and fails loudly
rather than reusing a verdict made for different input. Set `PARALLAX_SMOKE_RECORD_KEY` to an OpenRouter
key to forward to OpenRouter and rewrite the recording.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

UPSTREAM = "https://openrouter.ai/api/v1"
RECORDING = Path(__file__).parent / "fixtures" / "judge-replay.json"


def trim(reply: object) -> object:
    """Drop a fallback reply's reasoning and provider metadata: the judge reads only content, id and usage."""
    if not isinstance(reply, dict) or "choices" not in reply:
        return reply
    choices = [
        {**choice, "message": {k: v for k, v in choice["message"].items() if k in ("role", "content")}}
        for choice in reply["choices"]
    ]
    return {**reply, "choices": choices}


def request_key(path: str, body: bytes) -> str:
    canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{path}\n{canonical}".encode()).hexdigest()


class JudgeReplay:
    def __init__(self, record_key: str | None) -> None:
        self.record_key = record_key
        self.recorded: dict[str, dict[str, object]] = {} if record_key else json.loads(RECORDING.read_text())
        self.misses: list[str] = []
        self.app = Starlette(routes=[Route("/{path:path}", self.answer, methods=["POST"])])

    async def answer(self, request: Request) -> Response:
        path = request.path_params["path"]
        body = await request.body()
        key = request_key(path, body)
        if self.record_key is not None:
            async with httpx.AsyncClient(timeout=60) as client:
                upstream = await client.post(
                    f"{UPSTREAM}/{path}",
                    content=body,
                    headers={"Authorization": f"Bearer {self.record_key}", "Content-Type": "application/json"},
                )
            self.recorded[key] = {"path": path, "status": upstream.status_code, "reply": upstream.json()}
            return Response(upstream.content, status_code=upstream.status_code, media_type="application/json")
        entry = self.recorded.get(key)
        if entry is None:
            self.misses.append(path)
            return JSONResponse({"error": "no recorded reply for this exact request; re-record"}, status_code=418)
        return JSONResponse(entry["reply"], status_code=int(str(entry["status"])))

    def save(self) -> None:
        RECORDING.parent.mkdir(parents=True, exist_ok=True)
        trimmed = {key: {**entry, "reply": trim(entry["reply"])} for key, entry in self.recorded.items()}
        RECORDING.write_text(json.dumps(trimmed, indent=1, sort_keys=True) + "\n")
