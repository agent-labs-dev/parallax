# Runtime verification: Hermes and OpenClaw

Offline runtime probes of the current Hermes and OpenClaw binaries against a
controlled local fake upstream (OpenAI + Anthropic wires) and the real parallax
world MCP server, routed through the real parallax meter. No paid provider,
judge, or evaluation call is made, and no real model is executed.

What is real here: the installed Hermes / OpenClaw binaries, the parallax meter
and world servers, and every request/result they exchange. What is synthetic:
the upstream responses (`fake_upstream.py` is a scripted tool-loop responder, so
it exercises the harness's protocol paths without a model). No semantic quality
or conformance conclusion is drawn from these synthetic responses; the results
below are protocol-level facts only.

## Verified (protocol level)

| Area | Hermes 0.22.6 | OpenClaw 2026.10.10 |
|---|---|---|
| Model routing (OpenAI wire) | `-m gpt-x` -> `POST /v1/chat/completions` | `meter-openai/gpt-x` -> `POST /v1/chat/completions` |
| Model routing (Anthropic wire) | `-m claude-sonnet-5` -> `POST /v1/messages` | `meter-anthropic/claude-sonnet-5` -> `POST /v1/messages` |
| Auth isolation | trial key to meter; meter swaps in upstream key | same (observed `fake-openai-key` / `fake-anthropic-key`) |
| Cost metering | meter records usage; unpriced model -> `cost_usd: null` | same, both wires (`gpt-x` + `claude-sonnet-5`) |
| Sessions | `--resume local-<hex>` carries context | `--session-id` carries context (turn-2 history n_messages grows) |
| Memory injection | `$HERMES_HOME/memories/MEMORY.md` injected | workspace `USER.md` injected (sentinel seen in fresh session) |
| MCP world | deferred `tool_call` fails (finding 2); fixed by `tools.tool_search.enabled: off` (finding 2) | deferred `dispatch_action` reaches the world; echo canary passes (finding 3) |
| Web isolation | `agent.disabled_toolsets` honoured | `tools.deny` honoured (no `web_search`/`web_fetch`/`browser`) |

The web row above records only that the web tools are ABSENT from the inlined
tool list sent to the model, not that the harness was proven unable to reach the
network. Complete web isolation was not exercised offline.

## Findings

1. **Hermes 0.22.6 defers MCP tools behind `tool_search`/`tool_describe`/`tool_call`.**
   The single-shot `hermes chat -Q -q` request carries the built-in toolset plus
   the three bridge tools, but not the world MCP tool names. The bridge itself
   works: `tool_search` returns the real names
   (`mcp__world__chat_read_channel`, `mcp__world__chat_post_message`), and
   `tool_describe` returns their schemas. See
   `captured/hermes-toolsearch-bridge.json`.

2. **Hermes 0.22.6 deferred `tool_call` sends the namespaced tool name, and the
   config fix is `tools.tool_search.enabled: "off"`.** The deferred `tool_call`
   invokes the world tool by `mcp__world__chat_read_channel`, which the parallax
   world rejects (`unknown tool 'mcp__world__chat_read_channel'`; it advertises
   and accepts only `chat_read_channel`). Disabling the bridge
   (`tools.tool_search.enabled: "off"`) inlines the MCP tools and the direct
   dispatch maps back to the canonical name: the world then records
   `chat_read_channel` and `chat_post_message`, both `ok: true`. See
   `captured/world-tool-name-mismatch.json`,
   `captured/hermes-toolsearch-bridge.json`, and
   `captured/hermes-toolsearch-off-workaround.json`.

3. **OpenClaw 2026.10.10 also defers MCP and memory tools, but its bridge is
   correct.** The request carries 12 inlined core tools plus
   `tool_search`/`tool_describe`/`dispatch_action` (no inlined `world__*` or
   `memory_search`). `dispatch_action` maps `world__chat_read_channel` back to
   the canonical `chat_read_channel` and reaches the world: the echo canary
   passes. See `captured/openclaw-2026.10.10-dispatch-action.json` and
   `captured/openclaw-2026.10.10-matrix.json`.

4. **Hermes session id format changed** from `20260923_214905_35b9d0` to
   `local-<64-hex>`. The `hermes_quiet` parser still matches, and `--resume`
   still works.

5. **Hermes 0.22.6 probes context-window endpoints the meter does not allowlist**
   (`GET /api/v1/models`, `POST /api/show`, `GET /v1/props`, `/props`). Through
   the meter these are refused or 404 and hermes tolerates them; no tally impact.

6. **OpenClaw 2026.10.10 is pinned with Node 24.22.2.** Its package declares
   Node `>=24.16.0 <25 || >=26.1.0`. The manifest explicitly permits the
   package lifecycle scripts (`npm i -g openclaw@2026.10.10 --allow-scripts=openclaw`, or an
   `allow-scripts` entry in `.npmrc`/config). `postinstall-bundled-plugins.mjs`
   is a package-lifecycle cleanup (dist inventory / fs-safe prebuild), not a
   plugin installer: the world MCP and memory tools are compiled into `dist/`
   and are deferred, not absent.

7. **OpenClaw bootstraps the workspace** with its own `AGENTS.md`, `SOUL.md`,
   `IDENTITY.md`, `USER.md`, `BOOTSTRAP.md` and a `.git` directory on first run.

## Reproduction

`reproduce.sh` reproduces exactly two flows, end to end, and asserts the world
recorded `chat_read_channel` then `chat_post_message` (both ok) plus the posted
`heron-e4e298` text:

- Hermes 0.22.6 with `tools.tool_search.enabled: "off"` (inlined MCP -> world
  read/post).
- OpenClaw 2026.10.10 deferred `tool_search` -> `dispatch_action` (bridge ->
  world read/post).

Requirements: `python3`, the parallax repo `.venv` (`.venv/bin/python -m parallax.world` and `-m parallax.meter` are
invoked directly, no `uv` wrapper), `node`/`npm` (tested Node 24.22.2 for OpenClaw
2026.10.10), and the `hermes` / `openclaw` binaries on PATH (set `HERMES=` /
`OPENCLAW=` to override). The script binds a unique `mktemp -d` scratch dir
(claimed with `keeper claim` when available), preflights the ports with a socket
bind, records every started PID to `$SCRATCH/pids.txt`, and stops only those PIDs
on exit.

The broader matrix (session carry, memory injection, web-tool absence, both-wire
auth, and the manifest wrapper + parser) was captured by hand and is NOT covered
by `reproduce.sh`; see the `captured/*.json` files.

## OpenClaw manifest wrapper (verified by hand, not in reproduce.sh)

The exact manifest turn command (`openclaw agent --local --json` then
`openclaw sessions export-trajectory --session-key ... --workspace ... --output
turn --json` + the `--- openclaw trajectory events ---` marker + transcript
grep) was run against 2026.10.10 on the fake upstream. The export path
`$home/trajectory/.openclaw/trajectory-exports/turn/events.jsonl` is produced, the
marker and transcript events land on stdout, and the `openclaw_json` parser
parses the output to COMPLETED with the correct answer/session/model/usage. One
diagnostic note: because 2026.10.10 defers MCP tools, the transcript tool-call
steps are `tool_search`/`tool_describe`/`dispatch_action` (the bridge tools),
not the canonical `chat_read_channel`/`chat_post_message` (the underlying names
sit in `dispatch_action` arguments `id`, e.g. `world__chat_read_channel`). The
parser could unwrap those to the underlying names; it does not
today, so fix-brief tool diagnostics show the bridge names for 2026.10.10.

`captured/` holds `versions.txt`, the world tool-name mismatch evidence, the
hermes tool-search bridge evidence (failure + `enabled: off` workaround), and the
OpenClaw 2026.10.10 dispatch_action and session/memory/web/wire matrix evidence.
