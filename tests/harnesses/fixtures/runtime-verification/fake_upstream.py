#!/usr/bin/env python3
"""Controlled fake OpenAI/Anthropic upstream for offline harness runtime verification.

Serves just enough of the two vendor wires for Hermes and OpenClaw to complete the
scripted ``conf-mcp-echo`` tool loop, and logs every request it receives so the
caller can prove (a) the harness hit THIS upstream, never a real provider, (b) the
exact model field it sent, and (c) the auth it presented. It returns deterministic
usage so a metering proxy in front of it can price the turn.

Two MCP tool surfaces are supported:

- Inlined MCP (OpenClaw 2026.9.2): the world tools appear in the request as
  ``world__chat_read_channel`` / ``world__chat_post_message``. The upstream calls
  them directly.

- Bridged MCP (Hermes 0.22.6): the world tools are deferred behind
  ``tool_search`` -> ``tool_describe`` -> ``tool_call``. The upstream drives that
  exact native sequence, reads the real tool names out of the ``tool_search``
  result, and then invokes the world tools through ``tool_call``.

Endpoints (relative to the base path, e.g. ``/v1``):

    POST /v1/chat/completions            OpenAI wire (stream and non-stream)
    GET  /v1/models                      OpenAI wire model listing
    POST /v1/messages                    Anthropic wire (stream and non-stream)
    POST /v1/messages/count_tokens       Anthropic wire token counting

The check-in code is hard-coded to the conformance seed (``heron-e4e298``) so the
run is deterministic and needs no model.

Env:
    FAKE_UPSTREAM_LOG      JSONL path to append one request record per request
                           (default ./fake_upstream.log).
    FAKE_UPSTREAM_SENTINEL if set, each record gains ``sentinel_seen`` for whether
                           the sentinel appears in the request body.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHECKIN_CODE = "heron-e4e298"
CLOSING_TEXT = "Posted in #conf. Dana's check-in code was heron-e4e298."

LOG_PATH = os.environ.get("FAKE_UPSTREAM_LOG", "fake_upstream.log")
_log_lock = threading.Lock()


def log_request(method: str, path: str, headers, body: dict | None) -> None:
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    api_key = headers.get("x-api-key") or ""
    if auth:
        scheme, _, value = auth.partition(" ")
    else:
        scheme, value = ("x-api-key", api_key) if api_key else ("", "")
    record = {
        "ts": time.time(),
        "method": method,
        "path": path,
        "auth_scheme": scheme or None,
        "auth_value": value or None,
        "model": (body or {}).get("model"),
        "tools": [t.get("name") or t.get("function", {}).get("name") for t in (body or {}).get("tools", []) or []],
        "stream": (body or {}).get("stream"),
        "n_messages": len((body or {}).get("messages", []) or []),
    }
    sentinel = os.environ.get("FAKE_UPSTREAM_SENTINEL")
    if sentinel:
        record["sentinel_seen"] = sentinel in json.dumps(body or {}, sort_keys=True)
    with _log_lock, open(LOG_PATH, "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _canonical(name: str) -> str:
    """Strip a harness's MCP namespace (``mcp__world__`` / ``world__`` / ``world.`` / ``world/``)."""
    for prefix in ("mcp__world__", "mcp_world_", "world__", "world.", "world/"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _tool_names(body: dict) -> list[str]:
    out: list[str] = []
    for t in body.get("tools", []) or []:
        if isinstance(t, dict):
            out.append(t.get("name") or t.get("function", {}).get("name"))
    return [n for n in out if n]


def _find_tool(body: dict, canonical: str) -> str | None:
    """The actual (namespaced) name of a tool whose canonical name matches *canonical*."""
    for name in _tool_names(body):
        if _canonical(name) == canonical:
            return name
    return None


def _record_tool_call(name: str, raw_args, invoked: set[str]) -> None:
    if not name:
        return
    invoked.add(name)
    canon = _canonical(name)
    if canon not in ("tool_call", "dispatch_action"):
        return
    if isinstance(raw_args, str):  # OpenAI wire: arguments arrive as a JSON string
        try:
            raw_args = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            raw_args = None
    if not isinstance(raw_args, dict):
        return
    if canon == "tool_call":  # hermes: {"calls": [{"name", "arguments"}]}
        for c in raw_args.get("calls", []) or []:
            if isinstance(c, dict) and c.get("name"):
                invoked.add(c["name"])
    else:  # openclaw: {"id", "args"}
        if isinstance(raw_args.get("id"), str):
            invoked.add(raw_args["id"])


def _extract(messages: list) -> tuple[set[str], list[str]]:
    """(canonical names of tools actually invoked, every tool-result content string).

    Only assistant-initiated calls count as "invoked"; a name appearing inside a
    ``tool_search`` / ``tool_describe`` result is a description, not a call, so it
    is kept out of the invoked set and read separately by ``_world_names``.
    """
    invoked: set[str] = set()
    result_texts: list[str] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            for block in m.get("content") if isinstance(m.get("content"), list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    _record_tool_call(block.get("name", ""), block.get("input"), invoked)
            for tc in m.get("tool_calls", []) or []:
                fn = (tc or {}).get("function", {})
                _record_tool_call(fn.get("name", ""), fn.get("arguments"), invoked)
        elif role == "tool":
            result_texts.append(str(m.get("content", "")))
        elif role == "user":
            for block in m.get("content") if isinstance(m.get("content"), list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_texts.append(json.dumps(block.get("content", "")))
    return {_canonical(c) for c in invoked}, result_texts


_WORLD_TOOL_RE = re.compile(r"[A-Za-z0-9_]*(?:chat_read_channel|chat_post_message)[A-Za-z0-9_]*")


def _world_names(result_texts: list[str]) -> tuple[str | None, str | None]:
    """The exact world chat tool names the harness reported via tool_search / tool_describe."""
    read_name: str | None = None
    post_name: str | None = None
    for text in result_texts:
        for m in _WORLD_TOOL_RE.findall(text):
            if "chat_read_channel" in m and read_name is None:
                read_name = m
            if "chat_post_message" in m and post_name is None:
                post_name = m
    return read_name, post_name


def _plan(body: dict) -> tuple[str, str | None, dict | None]:
    """(kind, name, args): kind in {"tool", "text"} describing the next assistant action."""
    invoked, result_texts = _extract(body.get("messages", []) or [])

    search_name = _find_tool(body, "tool_search")
    if search_name is not None:
        # Bridged MCP: drive tool_search -> tool_describe -> invoke. Two bridge dialects
        # exist: hermes (tool_call with {"calls":[{name,arguments}]}) and openclaw 2026.10.10
        # (dispatch_action with {"id","args"}).
        describe_name = _find_tool(body, "tool_describe")
        call_name = _find_tool(body, "tool_call")
        dispatch_name = _find_tool(body, "dispatch_action")
        openclaw = dispatch_name is not None and call_name is None
        if "tool_search" not in invoked:
            queries = (
                [{"query": "chat read channel messages"}, {"query": "chat post message"}]
                if openclaw
                else ["chat read channel messages", "chat post message"]
            )
            return "tool", search_name, {"queries": queries}
        read_name, post_name = _world_names(result_texts)
        if "tool_describe" not in invoked and describe_name and (read_name or post_name):
            if openclaw:
                return "tool", describe_name, {"id": read_name or post_name}
            names = [n for n in (read_name, post_name) if n]
            return "tool", describe_name, {"names": names}
        invoke_name = dispatch_name if openclaw else call_name
        if invoke_name and read_name and "chat_read_channel" not in invoked:
            if openclaw:
                return "tool", invoke_name, {"id": read_name, "args": {"channel": "conf"}}
            return "tool", invoke_name, {"calls": [{"name": read_name, "arguments": {"channel": "conf"}}]}
        if invoke_name and post_name and "chat_post_message" not in invoked:
            if openclaw:
                return (
                    "tool",
                    invoke_name,
                    {"id": post_name, "args": {"channel": "conf", "text": f"Check-in code: {CHECKIN_CODE}"}},
                )
            return (
                "tool",
                invoke_name,
                {
                    "calls": [
                        {"name": post_name, "arguments": {"channel": "conf", "text": f"Check-in code: {CHECKIN_CODE}"}}
                    ]
                },
            )
        return "text", None, None

    # Inlined MCP (openclaw 2026.9.2): call the world tools directly.
    read_name = _find_tool(body, "chat_read_channel")
    post_name = _find_tool(body, "chat_post_message")
    if read_name and "chat_read_channel" not in invoked:
        return "tool", read_name, {"channel": "conf"}
    if post_name and "chat_read_channel" in invoked and "chat_post_message" not in invoked:
        return "tool", post_name, {"channel": "conf", "text": f"Check-in code: {CHECKIN_CODE}"}
    return "text", None, None


def _usage() -> dict:
    return {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}


def _tool_call_delta(name: str, args: dict) -> dict:
    return {"name": name, "arguments": json.dumps(args)}


def _openai_chunks(model: str, kind: str, name: str | None, args: dict | None, call_id: str) -> list[dict]:
    if kind == "tool":
        delta = {
            "role": "assistant",
            "tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": _tool_call_delta(name, args)}],
        }
    else:
        delta = {"role": "assistant", "content": CLOSING_TEXT}
    finish = {"role": "assistant", "content": ""}
    return [
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        },
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": finish, "finish_reason": "stop"}],
            "usage": _usage(),
        },
    ]


def _openai_nonstream(model: str, kind: str, name: str | None, args: dict | None, call_id: str) -> dict:
    if kind == "tool":
        msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": _tool_call_delta(name, args)}],
        }
    else:
        msg = {"role": "assistant", "content": CLOSING_TEXT}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": {
            **_usage(),
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _anthropic_blocks(kind: str, name: str | None, args: dict | None, call_id: str) -> tuple[list[dict], dict]:
    if kind == "tool":
        blocks = [{"type": "tool_use", "id": call_id, "name": name, "input": args or {}}]
    else:
        blocks = [{"type": "text", "text": CLOSING_TEXT}]
    usage = {"input_tokens": 40, "output_tokens": 12, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    return blocks, usage


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return None

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, chunks):
        body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
        body += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_anthropic_sse(self, events):
        body = b"".join(
            b"event: " + ev.encode() + b"\ndata: " + json.dumps(data).encode() + b"\n\n" for ev, data in events
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            log_request("GET", self.path, self.headers, None)
            self._send_json(
                {
                    "object": "list",
                    "data": [
                        {"id": self.headers.get("x-fake-model", "fake/model"), "object": "model", "owned_by": "fake"}
                    ],
                }
            )
            return
        self._send_json({"error": {"message": f"GET {self.path} not served by the fake upstream"}}, 404)

    def do_POST(self):
        body = self._read_body()
        path = self.path.rstrip("/")
        model = (body or {}).get("model") or "fake/model"
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        kind, name, args = _plan(body or {})
        if os.environ.get("FAKE_UPSTREAM_DEBUG"):
            with _log_lock, open(LOG_PATH + ".debug", "a") as f:
                f.write(
                    json.dumps(
                        {"path": path, "plan": kind, "name": name, "args": args, "messages": body or {}}, sort_keys=True
                    )
                    + "\n"
                )
        log_request("POST", self.path, self.headers, body)

        if path.endswith("/chat/completions"):
            if (body or {}).get("stream"):
                self._send_sse(_openai_chunks(model, kind, name, args, call_id))
            else:
                self._send_json(_openai_nonstream(model, kind, name, args, call_id))
            return

        if path.endswith("/messages"):
            blocks, usage = _anthropic_blocks(kind, name, args, call_id)
            if (body or {}).get("stream"):
                mid = f"msg_{uuid.uuid4().hex[:12]}"
                if kind == "tool":
                    block_start = {"type": "tool_use", "id": call_id, "name": name, "input": {}}
                    block_delta = {"type": "input_json_delta", "partial_json": json.dumps(args or {})}
                else:
                    block_start = {"type": "text", "text": ""}
                    block_delta = {"type": "text_delta", "text": CLOSING_TEXT}
                stop_reason = "tool_use" if kind == "tool" else "end_turn"
                self._send_anthropic_sse(
                    [
                        (
                            "message_start",
                            {
                                "type": "message_start",
                                "message": {
                                    "id": mid,
                                    "type": "message",
                                    "role": "assistant",
                                    "model": model,
                                    "content": [],
                                    "stop_reason": None,
                                    "stop_sequence": None,
                                    "usage": {"input_tokens": 40, "output_tokens": 0},
                                },
                            },
                        ),
                        (
                            "content_block_start",
                            {"type": "content_block_start", "index": 0, "content_block": block_start},
                        ),
                        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": block_delta}),
                        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                        (
                            "message_delta",
                            {
                                "type": "message_delta",
                                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                "usage": {"output_tokens": 12},
                            },
                        ),
                        ("message_stop", {"type": "message_stop"}),
                    ]
                )
            else:
                self._send_json(
                    {
                        "id": f"msg_{uuid.uuid4().hex[:12]}",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": blocks,
                        "stop_reason": "tool_use" if kind == "tool" else "end_turn",
                        "usage": usage,
                    }
                )
            return

        if path.endswith("/messages/count_tokens"):
            self._send_json({"input_tokens": 40})
            return

        self._send_json({"error": {"message": f"POST {self.path} not served by the fake upstream"}}, 404)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=19000)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fake upstream listening on {args.host}:{args.port} (log={LOG_PATH})", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
