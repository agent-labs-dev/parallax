#!/usr/bin/env bash
# Offline runtime reproduction: two successful echo loops.
#   (1) Hermes 0.22.6 with tools.tool_search.enabled="off"  -> inlined MCP, world records read+post
#   (2) OpenClaw 2026.10.10 deferred dispatch_action         -> bridge, world records read+post
# Both run against the local fake upstream (fake_upstream.py) and the real parallax
# world MCP server, through the real parallax meter. No paid provider/judge call; every
# token is a synthetic trial key or a local fake key. No Claude is executed.
#
# This script covers exactly the two flows above. The broader matrix (session carry,
# memory injection, web-isolation tool list, both-wire auth) was captured by hand and
# lives in captured/; see README.md for the distinction.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
PYTHON="$REPO/.venv/bin/python"             # direct interpreter, no uv child wrapper
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/parallax-rv.XXXXXX")"
if command -v keeper >/dev/null 2>&1; then keeper claim "$SCRATCH" >/dev/null 2>&1 || true; fi
LOGS="$SCRATCH/logs"
mkdir -p "$LOGS" "$SCRATCH/hermes-home" "$SCRATCH/openclaw-home" "$SCRATCH/workspace"
echo "scratch: $SCRATCH"
cd "$REPO"   # relative task paths (tasks/conformance/...) resolve from the repo root

FAKE_PORT=19000
METER_PORT=19001
WORLD_PORT=19002
ADMIN=admintok
FAKE_OPENAI_KEY=fake-openai-key
FAKE_ANTHROPIC_KEY=fake-anthropic-key
FAKE_UPSTREAM_LOG="$LOGS/fake-upstream.jsonl"

HERMES="${HERMES:-hermes}"
OPENCLAW="${OPENCLAW:-openclaw}"

# ---- owned-PID bookkeeping: record exact PIDs, kill+wait only those ----
PIDS=()
record_pid() { PIDS+=("$1"); printf '%s %s\n' "$1" "$2" >> "$SCRATCH/pids.txt"; }

remove_pid() {  # $1 pid: drop it from the active list so cleanup never re-signals it
  local p="$1" q new=()
  for q in "${PIDS[@]}"; do [[ "$q" == "$p" ]] || new+=("$q"); done
  PIDS=("${new[@]}")
}

stop_owned() {  # $1 pid: kill, wait, then remove from the active list
  kill "$1" 2>/dev/null || true
  wait "$1" 2>/dev/null || true
  remove_pid "$1"
}

cleanup() {
  for pid in "${PIDS[@]}"; do stop_owned "$pid"; done
  # scratch dir (with pids.txt and logs) is left in place for inspection.
}
trap cleanup EXIT

# ---- port preflight: bind-check each port, refusing on OSError ----
preflight_ports() {
  python3 - "$FAKE_PORT" "$METER_PORT" "$WORLD_PORT" <<'PY'
import socket, sys
for p in sys.argv[1:]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # ignore TIME_WAIT, detect live listeners
    try:
        s.bind(("127.0.0.1", int(p)))
    except OSError as e:
        print(f"ERROR: port {p} already bound ({e}); refusing to start", file=sys.stderr)
        sys.exit(1)
    finally:
        s.close()
PY
}
preflight_ports

# ---- readiness helpers (poll, no fixed sleeps) ----
wait_url() {  # $1 url  $2 label
  for _ in $(seq 1 200); do
    if curl -sf -m 1 "$1" >/dev/null 2>&1; then return 0; fi
    sleep 0.1
  done
  echo "ERROR: $2 not ready at $1" >&2
  return 1
}

wait_file_line() {  # $1 file  $2 pattern  $3 label
  for _ in $(seq 1 200); do
    if grep -qE "$2" "$1" 2>/dev/null; then return 0; fi
    sleep 0.1
  done
  echo "ERROR: $3 not ready (no '$2' in $1)" >&2
  return 1
}

start_world() {  # sets WORLD_TOKEN, WORLD_CTRL, WORLD_PID
  "$PYTHON" -m parallax.world serve --task tasks/conformance/conf-mcp-echo --http "127.0.0.1:$WORLD_PORT" \
    >"$LOGS/world.out" 2>"$LOGS/world.stderr" &
  WORLD_PID=$!
  record_pid "$WORLD_PID" "world"
  wait_file_line "$LOGS/world.stderr" '^token: ' "world server"
  WORLD_TOKEN="$(sed -nE 's/^token: //p' "$LOGS/world.stderr")"
  WORLD_CTRL="$(sed -nE 's/^control token: //p' "$LOGS/world.stderr")"
}

# ---- 1. fake upstream (OpenAI + Anthropic wires, logs every request) ----
FAKE_UPSTREAM_LOG="$FAKE_UPSTREAM_LOG" python3 "$HERE/fake_upstream.py" --host 127.0.0.1 --port "$FAKE_PORT" \
  >"$LOGS/fake-upstream.out" 2>&1 &
record_pid "$!" "fake-upstream"
wait_url "http://127.0.0.1:$FAKE_PORT/v1/models" "fake upstream"

# ---- 2. meter (forwards to the fake upstream) ----
PARALLAX_METER_ADMIN_TOKEN="$ADMIN" \
PARALLAX_UPSTREAM_OPENAI="http://127.0.0.1:$FAKE_PORT/v1" \
PARALLAX_UPSTREAM_ANTHROPIC="http://127.0.0.1:$FAKE_PORT/v1" \
PARALLAX_UPSTREAM_OPENAI_KEY="$FAKE_OPENAI_KEY" \
PARALLAX_UPSTREAM_ANTHROPIC_KEY="$FAKE_ANTHROPIC_KEY" \
"$PYTHON" -m parallax.meter "127.0.0.1:$METER_PORT" >"$LOGS/meter.out" 2>&1 &
record_pid "$!" "meter"
wait_url "http://127.0.0.1:$METER_PORT/_health" "meter"

register() { curl -sf -X PUT "http://127.0.0.1:$METER_PORT/_meter/$ADMIN/$1" -o /dev/null; }
world_evidence() { curl -sf "http://127.0.0.1:$WORLD_PORT/_control/$WORLD_CTRL/evidence"; }

# Assert the world received exactly chat_read_channel then chat_post_message (both ok)
# and that the resulting posted message carries the check-in code.
assert_world_echo() {  # $1 harness label
  local evidence
  evidence="$(world_evidence)"
  python3 - "$1" "$evidence" <<'PY'
import json, sys
label = sys.argv[1]
d = json.loads(sys.argv[2])
calls = [(c["tool"], c["ok"]) for c in d["calls"]]
posted = [m["text"] for m in d["diff"]["tables"]["chat.messages"]["added"]]
expected = [("chat_read_channel", True), ("chat_post_message", True)]
ok = calls == expected and any("heron-e4e298" in t for t in posted)
print(f"{label}: world calls {calls}, posted {posted}")
if not ok:
    print(f"ASSERTION FAILED for {label}: expected {expected}, got calls {calls}, posted {posted}",
          file=sys.stderr)
    sys.exit(1)
PY
}

# =====================================================================================
# (1) Hermes: tools.tool_search.enabled="off" -> inlined MCP -> world read+post
# =====================================================================================
start_world
register hermes
cat > "$SCRATCH/hermes-home/config.yaml" <<EOF
model: { default: "gpt-x", provider: "meter-openai" }
providers:
  meter-openai: { base_url: "http://127.0.0.1:$METER_PORT/hermes/openai/v1", api_key: "hermes", api_mode: "chat_completions" }
  meter-anthropic: { base_url: "http://127.0.0.1:$METER_PORT/hermes/anthropic", api_key: "hermes", api_mode: "anthropic_messages" }
agent: { disabled_toolsets: ["web", "browser"] }
security: { tirith_enabled: false }
tools: { tool_search: { enabled: "off" } }
mcp_servers:
  world:
    url: "http://127.0.0.1:$WORLD_PORT/mcp"
    headers: { Authorization: "Bearer $WORLD_TOKEN" }
EOF
HOME="$SCRATCH/home" HERMES_HOME="$SCRATCH/hermes-home" \
  "$HERMES" chat -Q --yolo --source tool -m gpt-x \
    -q "Dana left today's check-in code in #conf. Post it back in #conf, then tell me the code." \
    >"$LOGS/hermes.stdout" 2>"$LOGS/hermes.stderr"
echo "== hermes answer =="; cat "$LOGS/hermes.stdout"
assert_world_echo "hermes"
stop_owned "$WORLD_PID"

# =====================================================================================
# (2) OpenClaw: deferred tool_search -> dispatch_action -> world read+post
# =====================================================================================
start_world
register openclaw
cat > "$SCRATCH/openclaw-home/openclaw.json" <<EOF
{
  "agents": {"defaults": {"workspace": "$SCRATCH/workspace", "model": {"primary": "meter-openai/gpt-x"}}},
  "memory": {"search": {"provider": "none"}},
  "models": {"providers": {
    "meter-openai": {"baseUrl": "http://127.0.0.1:$METER_PORT/openclaw/openai/v1", "apiKey": "openclaw", "api": "openai-completions", "models": [{"id": "gpt-x", "name": "gpt-x"}]},
    "meter-anthropic": {"baseUrl": "http://127.0.0.1:$METER_PORT/openclaw/anthropic", "apiKey": "openclaw", "api": "anthropic-messages", "models": [{"id": "gpt-x", "name": "gpt-x"}]}
  }},
  "mcp": {"servers": {"world": {"url": "http://127.0.0.1:$WORLD_PORT/mcp", "transport": "streamable-http", "headers": {"Authorization": "Bearer $WORLD_TOKEN"}}}},
  "tools": {"deny": ["web_search", "web_fetch", "x_search", "browser", "message", "ask_user", "automations", "nodes", "dir_list"], "web": {"search": {"enabled": false}}}
}
EOF
OPENCLAW_STATE_DIR="$SCRATCH/openclaw-home" \
  "$OPENCLAW" agent --local --json --model "meter-openai/gpt-x" --session-id "echo" \
    --message "Dana left today's check-in code in #conf. Post it back in #conf, then tell me the code." \
    >"$LOGS/openclaw.json" 2>"$LOGS/openclaw.stderr"
echo "== openclaw answer =="; python3 -c 'import json; d=json.load(open("'"$LOGS"'/openclaw.json")); print([p.get("text") for p in d.get("payloads",[])])'
assert_world_echo "openclaw"

echo "== meter reports =="
echo "hermes:  $(curl -sf "http://127.0.0.1:$METER_PORT/_meter/$ADMIN/hermes")"
echo "openclaw:$(curl -sf "http://127.0.0.1:$METER_PORT/_meter/$ADMIN/openclaw")"
echo "== fake upstream requests =="; cat "$FAKE_UPSTREAM_LOG"
