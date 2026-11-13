# Harness adapters

A harness is the agent product under test: its loop, prompts, tools, memory and
model routing. Parallax treats every harness as a black box. The runner hands it
one turn at a time and reads back an answer. It grades the world the harness
acted on, and never the harness's own account of what it did.

- [Protocol](#protocol)
- [Two ways to add a harness](#two-ways-to-add-a-harness)
- [Plugins](#plugins)
- [Manifests](#manifests)
- [Mounting the world](#mounting-the-world)
- [Metering and cost](#metering-and-cost)
- [Checking a new harness](#checking-a-new-harness)
- [Harness matrix](#harness-matrix)

## Protocol

```python
class Harness(Protocol):
    info: HarnessInfo
    async def start(self, trial: Trial, box: BoxHandle) -> None: ...
    async def turn(self, t: TurnInput) -> TurnOutput: ...
    async def stop(self) -> None: ...          # idempotent; never deletes evidence
    async def version(self, box: BoxHandle) -> str: ...
```

**`Trial`** is what the harness receives when it starts:

- the requested model (`provider/model`, or `None` for product rows);
- the world MCP endpoint and its bearer-token variable;
- the per-trial `workspace` and `home`;
- the meter's OpenAI and Anthropic base URLs and a per-trial key.

By default Parallax sets no time or cost limit on the agent: a turn lasts until
the product ends it, under the product's own limits, and the adapter waits for
that. With `--turn-timeout`, Parallax cancels a turn still running at the
deadline and records a timeout. `stop` still runs afterwards, so release
anything a cancelled turn holds there.

When cost is metered, real provider keys never enter the box.

**`TurnInput`** has four fields:

- `index`;
- `text`;
- `new_session`;
- `attachments`, given as workspace-relative paths.

`new_session=True` means the harness starts a fresh conversation but keeps
`home`. Harness-native memory therefore survives across sessions, which is what
the `memory` capability measures.

**`TurnOutput`** has these fields:

- `status`: `completed`, `harness_error` or `timeout` (the product reported its own timeout);
- the final `answer`;
- a `session_id`;
- diagnostic `steps`;
- `usage`;
- `ttft_ms`;
- `actual_model`.

Steps are stored for diagnosis, but they are never graded. If a turn
does not complete, the trial ends early.

**Capabilities** are `mcp`, `workspace`, `shell`, `desktop`, `multi_turn`, `memory`
and `world_clock`.
`HarnessInfo.capabilities` is what the harness declares; the
[conformance canaries](#checking-a-new-harness) show whether it holds.

## Two ways to add a harness

1. **A CLI manifest.** Use this when the harness has a headless mode that prints
   a parseable final result. It is a TOML file, with no code.
2. **A Python adapter.** Use this for SDKs, remote APIs and long-lived bridges.
   Implement the protocol and register the class under the `parallax.harnesses`
   entry point from your own package:

   ```toml
   [project.entry-points."parallax.harnesses"]
   my-agent = "my_pkg.harness:MyHarness"
   ```

Harnesses resolve in this order: built-in manifests, then entry points, then the
controls (`null-claim`, `null-refuse`, `oracle`). An unknown name fails with
the list of known ones.

To add a manifest, copy the closest one in `parallax/harnesses/manifests/` to
`<name>.toml`, then:

1. Save `<bin> --help` as `help.txt` in `tests/harnesses/fixtures/<name>/`.
2. Record one or two real turns there, so the parser test has real output.
3. Run the [conformance canaries](#checking-a-new-harness).

## Plugins

A product that needs more than a harness ships a plugin package. Each group
below is an entry point that core loads on demand and never imports directly:

| Group | Entry point names | Contract |
|---|---|---|
| `parallax.harnesses` | a harness name | a `Harness` class |
| `parallax.task_extensions` | the extension's `name` | a `TaskExtension`: a model for `[metadata.<name>]`, lane rules, an optional load check (`check_absent=True` also checks tasks without the block), `real_accounts` for tasks that act on real owned accounts (exempt from the synthetic-address lint), and an optional `GradingPolicy` that grades the extension's tasks from the product state its harness reports as `trial_metadata`: its `checks` replace the task's, its `evidence` builds what the judge sees, and its versioned `id` joins the run's grading policy, so runs graded by different policy versions never compare |
| `parallax.suites` | the suite name | a callable returning the suite's root directories |
| `parallax.provenance` | a prefix | a callable `(run_dir) -> dict[str, str]`, recorded as `<prefix>.<key>` |

## Manifests

A manifest is a `ManifestSpec`:

```toml
protocol = "cli"
parser = "hermes_quiet"
session = "from_output"          # or "preassigned": the runner mints {session} per new session
stdin = "none"                   # or "prompt" to pipe the prompt on stdin
argv_first = ["hermes", "chat", "-Q", "-m", "{model_name}", "-q", "{prompt}"]
argv_next  = ["hermes", "chat", "-Q", "-m", "{model_name}", "--resume", "{session}", "-q", "{prompt}"]
status_note = ""                 # e.g. "unverified: not installed in reference env"

[info]
name = "hermes"
kind = "open"                    # open | product | control
capabilities = ["mcp", "workspace", "shell", "multi_turn", "memory"]
pins_model = true
cost_source = "metered"          # metered | harness | none
install = "pip install hermes-agent"   # RUN line for the box image
version_cmd = ["hermes", "--version"]
web_disabled_by = "config"       # flag | config | none
source_url = "https://github.com/NousResearch/hermes-agent"

[env]
HERMES_HOME = "{home}/.hermes"

[[config]]
path = "home:.hermes/config.yaml" # "home:<rel>" or "workspace:<rel>"
format = "yaml"                   # json | toml | yaml
content = { model = { default = "{model_name}", provider = "meter-openai" }, providers = { "meter-openai" = { base_url = "{meter_openai}", api_key = "{meter_key}" } }, mcp_servers = { world = { url = "{world_url}", headers = { Authorization = "Bearer {world_token}" } } } }
```

**Placeholders** are substituted into argv and config strings as typed values,
never through a shell. An unknown placeholder is an error. The full set is:

`{model}`, `{model_provider}`, `{model_name}`, `{prompt}`, `{prompt_file}`,
`{session}`, `{workspace}`, `{home}`, `{world_url}`, `{world_token}`,
`{world_token_env}`, `{meter_openai}`, `{meter_anthropic}`, `{meter_key}`.

**Continuation.** If `argv_next` is empty, the harness cannot declare
`multi_turn`.

**Parsers** make up a closed set of pure functions that turn stdout and stderr
into a turn result. They are tested against recorded fixtures:

- `openclaw_json`
- `hermes_quiet`
- `plain_text`

A parser should report the turn's tool calls as well as its answer: a judge reads an answer like
"Saved." with no tool call behind it as an unbacked claim. Hermes prints only its closing message,
so its manifest prints `hermes sessions export` after the answer, and `hermes_quiet` reads that
turn's calls from it.

If no parser fits a new CLI, add a `ParserId` member and its function. That is
the only code change a CLI harness can need.

Harness tool names such as `mcp__world__mail_send` are normalised to the
canonical `mail_send` for diagnostics. The world log always uses canonical names.

## Mounting the world

Every harness reaches one MCP server over streamable HTTP, and a bearer token
selects the trial's world. The URL depends on the isolation mode:

| Isolation | World URL seen by the harness | Web isolation |
|---|---|---|
| `docker` | `http://sidecar:7801/mcp`. The box joins only an internal network whose one other member is the sidecar, which runs the world and the meter. | `enforced` |
| `host` | `http://127.0.0.1:<port>/mcp`. Development only. | `flags`: native web tools are disabled by flag or config |
| `remote` | `<--world-public-url>/mcp`, through an operator tunnel, with a fresh 32-byte token per trial | `none`, or `flags` when the adapter switches the product's web off (`web_disabled_by`) |

A remote harness that declares `remote_box: device` gets a docker box for every
trial: the harness's own image, on
docker's default bridge network rather than the run's internal one, so a
daemon in it can reach the product. Snapshots, seed files and ownership work
as for any docker box. The box is not on the world's clock, because TLS to the
product would reject certificates issued after the world's date. The world
still reaches the product through the tunnel, and web isolation stays `none`
or `flags`: the product already reaches the internet, so the box's egress
weakens nothing that was declared. Such a product's agent has a shell whatever the
trial requires, and with no device of the trial's own it would run on the account's
primary device, which every trial shares, so a file one trial wrote there would be
waiting for the next. A harness that declares `remote_box: host` keeps a host
directory.

A `desktop` task adds an optional image layer with Xvfb, Openbox (window focus),
D-Bus, AT-SPI, GTK3/Python GI, and
[cua-driver 0.28.3](https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.28.3).
The Linux x86_64 binary is fetched by the image and checked against the release's
published SHA-256. The ordinary box Dockerfile, harness install line and image
identity formula are unchanged; desktop tags have a `-desktop` suffix. As usual,
source changes invalidate source-keyed images.

After seeding, the box starts `dbus-run-session`, Xvfb on `:99`, Openbox and the
Debian `/usr/libexec/at-spi-*` services, then launches the declared apps. It waits
for their initial windows before returning to the harness. Box commands join
that session with `DISPLAY=:99`, no `WAYLAND_DISPLAY`, `GTK_MODULES=gail:atk-bridge`,
`NO_AT_BRIDGE=0`, `GDK_BACKEND=x11`, and `SHELL=/bin/sh`. The daemon and its driver
child inherit the session bus. Session state and logs stay outside the workspace;
a failed startup removes the container. Trial teardown removes its desktop
processes with the container. See [authoring](authoring.md#desktop-apps) for
the task declaration and the Docker/MCP smoke command.

Under `remote` the runner serves the world on a random free port unless
`--world-port PORT` pins it, so a tunnel opened before the run can point at it:

```bash
cloudflared tunnel --url http://127.0.0.1:17801   # prints the public URL
parallax run core --harness my-product --isolation remote \
  --world-public-url https://<tunnel-host> --world-port 17801
```

`--world-port` is refused without `--isolation remote`, and the run stops with
an environment error when the port is already taken.

The world reaches each harness through that harness's own config:

- **hermes** gets an `mcp_servers` entry in its own config.
- **openclaw** gets an `openclaw.json` under `OPENCLAW_STATE_DIR`.
- **Remote product harnesses** from plugins register the world through the
  product's own API. They run outside a box, so each adapter reports every
  isolation gap it cannot close as a trial note; the plugin documents them.

A stdio-only harness can use `python -m parallax.world bridge --url U --token-env VAR`.

## Metering and cost

Each trial gets a key to the metering proxy:

- It forwards inference requests on the OpenAI and Anthropic wires to the
  upstreams set by `PARALLAX_UPSTREAM_{OPENAI,ANTHROPIC}` and `..._KEY`.
- It forwards only an allowlist of inference endpoints. On the OpenAI wire these
  are `POST chat/completions`, `POST responses`, `POST responses/input_tokens`
  and `GET models`. On the Anthropic wire they are `POST messages` and
  `POST messages/count_tokens`. Any other request, such as files, batches,
  fine-tuning or a stored-response read, gets HTTP 403 (`endpoint_not_allowed`)
  and is logged. It never reaches the provider. A `POST responses` with
  `background: true` is refused the same way, because it would generate after
  the request returns, outside the meter.
- It prices usage with `genai-prices`.
- It records the model that actually served each request.
- It never refuses a request for cost: spend is recorded, not capped. The
  product under test owns its own limits.

A manifest points the harness at the meter through `[env]`, for example
`ANTHROPIC_BASE_URL = "{meter_anthropic}"`.

| `cost_source` | Meaning | Rendered |
|---|---|---|
| `metered` | Priced by the meter from observed usage. The observed model is recorded | `M` |
| `harness` | The harness's own usage report, priced by Parallax or taken as the product states it | `H` |
| `none` | Unknown | `-`, never `$0` |

An unknown price is recorded as `None`, never as 0.

### Running real harnesses

A real run needs a provider key for each wire its models use. The meter runs on
the host, or in the docker sidecar, and reads these variables:

| Variable | Purpose |
|---|---|
| `PARALLAX_UPSTREAM_ANTHROPIC_KEY` | Real key for the Anthropic wire (Hermes and OpenClaw with `anthropic/...`) |
| `PARALLAX_UPSTREAM_ANTHROPIC` | Anthropic base URL, including `/v1`. Defaults to `https://api.anthropic.com/v1` |
| `PARALLAX_UPSTREAM_OPENAI_KEY` | Real key for the OpenAI wire (Hermes and OpenClaw with `openai/...`) |
| `PARALLAX_UPSTREAM_OPENAI` | OpenAI-compatible base URL, including `/v1`. Defaults to OpenRouter (`https://openrouter.ai/api/v1`) |

A wire without a key answers 503, so a run that uses only one wire needs only
that key. To call OpenAI directly instead of OpenRouter, set both OpenAI
variables:

```bash
export PARALLAX_UPSTREAM_ANTHROPIC_KEY=$ANTHROPIC_API_KEY
export PARALLAX_UPSTREAM_OPENAI=https://api.openai.com/v1
export PARALLAX_UPSTREAM_OPENAI_KEY=$OPENAI_API_KEY
parallax run conf-mcp-echo --harness hermes --model openai/gpt-6-sol --isolation docker
```

The keys never enter a box. A harness gets its trial's key and the meter's URL.
The meter swaps in the real key for allowlisted inference calls only. Harnesses
that do not use the meter (`cost_source = "harness"`) bring
their own credentials: the manifest lists host variable names under
`[info] credential_env` (for example `GEMINI_API_KEY`), and only those are copied
into the box. A metered harness cannot declare any, nor can a manifest name a
`PARALLAX_*` variable or the key of a provider the meter serves. Every row of
such a harness is flagged `unmetered (own credential)`, since Parallax never saw
its spend.

## Checking a new harness

Declared capabilities describe the intended surface. The `conformance` suite
holds one canary per capability; run it before comparing a new harness or a
new version of one:

```sh
parallax run conformance --harness openclaw --model anthropic/claude-sonnet-5.5
```

| Canary | Demonstrates |
|---|---|
| `conf-mcp-echo` | `mcp`: posts a nonce and repeats it |
| `conf-fault-then-retry` | `mcp`: retries past one injected `server_error` without duplicating |
| `conf-two-turn-carry` | `multi_turn` |
| `conf-memory-recall` | `memory` across a fresh session |
| `conf-workspace-edit-verify` | `workspace` and `shell`: makes a failing test pass |

A canary that fails points at the adapter, not the agent: fix the manifest, or
drop the capability it cannot demonstrate. A task that requires a capability
the harness does not declare is **NA** for that harness, never FAIL.

## Harness matrix

Support has three distinct levels:

- **Available**: an adapter or manifest is registered. Its declared capabilities
  describe the intended surface.
- **Offline tested**: scripted peers or fixtures exercise the adapter contract,
  including errors and cleanup. This does not verify the actual runtime.
- **Runtime protocol verified**: an identified runtime version has exercised
  the adapter against captured endpoints. A scripted model upstream verifies
  routing and tool transport, not model quality.

A status applies only to the version and configuration named in its evidence.
Historical captures do not verify a newer installation. The matrix retains
existing adapters, including coding products; launch integration work focuses
on general-purpose harnesses.

**Pins** means the adapter requests a particular model. Served-model evidence
comes from the meter, and an unobserved model remains unobserved. **Cost** names
its provenance; unknown or unpriced usage is never silently treated as free.

| Harness | Kind | Surface | Pins | Cost | Status |
|---|---|---|---|---|---|
| hermes | open | CLI `hermes chat -Q -q` | yes | metered | 0.22.6 runtime protocol probes against a scripted upstream; see evidence below |
| openclaw | open | CLI `openclaw agent --local --json` | yes | metered | 2026.10.10 runtime protocol probes; clean Docker install verified; see evidence below |
| oracle | control | Replays `solution/` | n/a | none | Control |
| null-claim / null-refuse | control | Fixed answer, no calls | n/a | none | Control |

All CLI flags are rechecked against `<bin> --help` whenever a manifest changes. A
flag that has drifted changes that manifest only.

General-purpose follow-ups are **Agno** and **Mastra**. Their integration
surface will be chosen from current official interfaces. See the
[roadmap](roadmap.md#harness-follow-ups).

## Hermes and OpenClaw runtime evidence

The [runtime probe captures](../tests/harnesses/fixtures/runtime-verification/README.md)
exercise installed Hermes 0.22.6 at source commit
`f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab` and OpenClaw 2026.10.10
with Node 24.22.2. A scripted local upstream supplies both OpenAI and Anthropic
wire responses. No model or quality judge runs in these probes.

The Hermes image installs that exact source revision with `uv==0.13.5`,
Python 3.14.9 and upstream locked dependencies in a separate editable environment.
Its version probe trusts only the immutable `/opt/hermes` Git checkout; without
that exception the unprivileged CLI displays `vunknown`. The probe reports 0.22.6.
OpenClaw is pinned to `openclaw@2026.10.10`, with its npm lifecycle scripts explicitly allowed.

Both CLIs route the requested model through the meter, which replaces the trial
key with the fake upstream key. Captures show session carry, memory-file
injection into a fresh session and authenticated world MCP read/write dispatch.
Unpriced synthetic models retain unknown cost. These are runtime protocol
checks, without new quality scores.

Hermes uses `tools.tool_search.enabled: "off"`: its deferred tool bridge at this
revision forwards a namespaced MCP name that the world rejects. The direct
path maps that name correctly. `HERMES_HOME` isolates its configuration,
sessions and `memories/MEMORY.md`; quiet output exposes the closing message
and session ID rather than tool steps.

OpenClaw uses the two explicit meter providers, FTS-only memory search and the
manifest's tool deny list. Its `dispatch_action` bridge reaches the canonical
world tool names. A fresh session receives workspace `USER.md`. Captures show
that web tools are absent from the inlined tool list; they do not establish
complete network isolation. Shell-capable harnesses require the Docker boundary
for workspace and process isolation. Host execution inherits host access.

Historical Hermes 0.19.0 and OpenClaw 2026.9.2 judgments remain version-specific;
they do not verify these installations.

## Browser harnesses

The browser package is a generic browser-agent surface for tasks whose
`metadata.parallax.requires` includes `browser`. Such tasks have no seeded world:
the harness receives the task prompt, drives its browser, and reports a final
page as `trial_metadata()["browser"]`. The evidence contains the final URL,
title, visible text, optional PNG, interactive controls, the answer, steps, and
citations. A plain CDP observer reads this evidence after the agent answers and
before browser cleanup. An observer plugin can replace it under the
`parallax.browser_observers` entry-point group; its name is part of the harness
configuration identity.

The built-in browser harness entry points are:

| Name | Runtime | Cost and credentials |
|---|---|---|
| `browser-use-cloud` | Hosted Cloud API, V3 or V4 | Provider-reported costs; `BROWSER_USE_API_KEY` |
| `browser-use-oss` | Optional Python browser worker | Browser and model costs from the worker; `BROWSER_USE_API_KEY` |
| `browser-harness` | Optional command worker attached to a Cloud browser | Browser cost plus worker-reported cost; `BROWSER_USE_API_KEY` |
| `cua-codex` | Optional desktop command worker | Worker-reported cost when available |

The caller selects the hosted model with the normal `--model` value. V4 preset
models are sent without provider parameters because the API rejects
`modelParams`, `mode`, and `harness` for presets. The adapter never writes a
preset identifier into source code.

Useful configuration keys are `PARALLAX_BROWSER_OBSERVER`,
`PARALLAX_BROWSER_CLOUD_API`, `PARALLAX_BROWSER_POLL_SECONDS`,
`PARALLAX_BROWSER_MODEL_PARAMS`, `PARALLAX_BROWSER_HARNESS_COMMAND`,
`PARALLAX_BROWSER_AGENT_COMMAND`, `PARALLAX_CUA_DRIVER`,
`PARALLAX_CUA_CODEX`, and `PARALLAX_CUA_COMMAND`. The optional dependencies
are installed with `parallax-evals[browser]`; competitor versions are recorded
in `parallax/harnesses/browser/pins.json` and checked with:

```sh
uv run python -m parallax.harnesses.browser.pins check
```

Timing uses the `session-ready-to-answer` basis. Browser opening and closing
are separate phases, and a provider or site outage returns a harness error
whose reason starts with `environment interruption: ` so it can be retried by
the runner.

## Device boxes

Device containers run Docker's tiny init process as PID 1 so a device daemon's
child process is reaped after shutdown. This is required for `local-device stop`
to observe a real exit rather than waiting on a zombie. They also carry their
runner's host, PID and process-start fingerprint.
Before allocating a device box, Parallax removes boxes from the same host whose
owner exited (including reused PIDs). This cleans up a killed runner on the next
device allocation without expiring a live trial. Older boxes without ownership
labels must be removed by hand; they are never guessed to be orphaned.
