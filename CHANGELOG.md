# Changelog

All notable changes to Parallax are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased]

### Fixed
 The error ends with the simulator gateway's host, such as `via ai-gateway.vercel.sh`, so a refusal names which endpoint and key refused it.
- tau2: a failed simulator call (a gateway outage or key limit) marks the trial an environment interruption that names the bridge's error, so it is retried and never graded. It used to end the trial `user_error`, scored as the agent's failure, or on the first turn INVALID for an empty transcript with the cause hidden under `tau2_user_error`.
- A failed `docker` command reports the tail of its output as well as its stderr, so a box image build that fails in a `RUN` step (such as the harness install) shows that step's error instead of only the legacy builder's deprecation notice.
- `parallax check --lint-only` no longer reports every converted task in `terminal-bench-2` and `tau2` for lacking an area prefix: a converted task keeps its upstream benchmark's id, and the prefix rule still applies to tasks we author.

## [2.0.0.dev62] - 2026-11-26

### Added

- `parallax compare --allow-config-change` pairs runs whose harness configs differ, for an A/B whose treatment is the config, and still refuses a judge, task, harness version or Parallax revision change.

### Fixed

- tau2: a bridge call that fails on a gateway refusal reports its HTTP status, so a key limit (HTTP 403) on the simulator reads as one instead of a bare `APIError`.

## [2.0.0.dev61] - 2026-11-25

### Fixed

- tau2: a simulated user that runs several tool calls in one turn (a telecom customer running two phone checks) no longer ends the trial `user_error`. The bridge built upstream's `MultiToolMessage` without its required `role`.
- tau2: a trial that ends `user_error` because the bridge refused the user turn records the bridge's error under `tau2_user_error`.

## [2.0.0.dev60] - 2026-11-25

### Fixed

- tau2 telecom gives the agent upstream's full policy: the main policy and the tech-support manual, wrapped in `<main_policy>` and `<tech_support_policy>` as upstream's environment builds it. Telecom tasks move to version 2, so runs on the earlier prompt never pair with later ones.

## [2.0.0.dev59] - 2026-11-25

### Fixed

- tau2 names its judge and user-simulator models by their OpenRouter ids (`openai/gpt-5.5`, `openai/gpt-5.4-mini`, `openai/gpt-4.1`), whose canonical slugs are the upstream snapshots, so the preflight finds them on the default gateway. tau2-bench calls them through LiteLLM's OpenAI-compatible provider at `PARALLAX_JUDGE_BASE_URL`, and each judge call is recorded under its gateway id.

## [2.0.0.dev58] - 2026-11-25

### Fixed

- The `browser-use-1-luna` arm now meets the other arms' conditions: Parallax opens its cloud browser on the shared 1120x780 screen with the explicit proxy and loads the task's start page before the agent clock starts. The agent answers to the task's `answer_schema` when the judge is shown one, the result carries the active page URL for the final-page observer, and the agent stops itself inside the turn, so a run at its limit keeps its cost (a run that reported none has an unknown model cost, not a free one). A browser that will not start, a site that does not answer, and browser-use's own transient model failures report `unavailable` and retry.

## [2.0.0.dev57] - 2026-11-25

### Added

- `PARALLAX_BROWSER_PROXY_COUNTRY` (a country code, or `none`) sets the proxy of every Browser Use cloud browser an arm uses: the V3 and V4 runs, browser-harness and agent-browser (`browsers.create`), and the browser-use library arms (`cloud_proxy_country_code`). It is sent explicitly, so no vendor default (the library's `us` proxy) decides it for one arm.

### Fixed

- Scrubbing a task secret never rewrites the host of the task's own site: a public demo credential that is also a hostname word (username `practice` on `practice.example.com`) no longer turns the host into `<secret>username</secret>.example.com`. The rest of the URL, any other host and all other text are still scrubbed.

## [2.0.0.dev56] - 2026-11-25

### Added

- `{next_monday}` (and the other weekday names) in a task prompt is the first such weekday strictly after the run's UTC date, so "next Monday" is one concrete date for every arm.

### Fixed

- The browser judge reads a task's prompt and summary with the same date placeholders filled in as the agent's prompt, from the run's recorded date, instead of the raw `{today+N}`.
- An undecided check beside a faulted run (harness error, timeout, over budget) keeps `execution incomplete: <status>` in the INVALID reason, and the row is retried under `--retry-invalid` whatever else is undecided, so an execution fault is retried the same way on every harness.
- A browser trial whose judge finds its evidence unavailable because the live site failed transiently (an error page, a 5xx or 429, an outage) is retried under `--retry-invalid` like a harness-reported outage, so no harness gets a second attempt another does not. The browser judge answers a typed `site_interruption` question for it, which changes the `browser-evidence-v2` judging identity; rows judged before do not pool with later ones.

## [2.0.0.dev55] - 2026-11-25

### Added

- The `terminal-bench-2` and `tau2` official suites, pinned by commit and graded by each benchmark's own verifier. Parallax owns each TB2 task container and the tau2 tool server, user simulator and transcript; a harness acts in the container with `live_container`. The codex and claude-code manifests run TB2 inside the container. tau2 needs `--isolation host` for a harness that would run in a docker box.
- The `browser-use-1-luna` harness: the open-source browser-use 1.0.8 library in Browser Use's fast configuration, with GPT-6 Luna through OpenRouter (`--model openai/gpt-6-luna`) at low reasoning effort on a hosted Browser Use browser. Its own judge is off because Parallax judges, and naming any other model marks the row invalid.
- A usage line keeps the billed component and the number of images its requests carried, as `component` and `images`, when a harness reports them. A browser agent that shows its decision model screenshots can then be compared by image-bearing calls alongside its tokens and dollars, per component. A line with no images omits the field, and earlier ledgers read unchanged.

### Fixed

- An official judge's retried call is metered on every openai SDK major (1.x to 3.x): each retry is recorded as an unpriced attempt where the SDK builds the request, so the judge's cost reads unknown instead of under-counted.
- The claude-code install line allows its own package's install script, so under npm 12, which blocks scripts by default, the Hyprland desktop box gets claude's native binary instead of a launcher that exits 1.
- A desktop recording that is not kept logs why: the stop step reports a missing recorder, one that will not stop, or one that wrote no file, with the recorder's log, instead of an empty reason.

## [2.0.0.dev54] - 2026-11-19

### Added

- `[metadata.parallax.desktop].session = "hyprland"`: the task's box runs deskpilot's standalone headless Hyprland session (Wayland, on the host's vkms display) from its own Arch Linux image, instead of Xvfb. The box is given the vkms node and nothing else, the Wayland variables reach every command, and `--videos` records the output with `wf-recorder`. The evals workflow loads vkms on a runner that can (not Blacksmith). `tasks/desktop-hyprland/files-desktop-hyprland-expense-form` is its canary.
- `[metadata.parallax.desktop].launch_by = "harness"`: the apps are commands the harness starts itself, so the box waits only for the session. The box gets an allowlisted egress for it: the sidecar runs a proxy (`parallax.egress`) that tunnels HTTPS to `openrouter.ai` and refuses everything else.
- `[metadata.parallax.desktop].packages`: the Debian packages a task's own desktop image installs above the shared GUI layer. The image is tagged by their list, so a task that needs an application (a 3D suite, an editor) carries its install cost alone and two tasks with different lists never share an image; the shared desktop tag and every other image are unchanged.
- Desktop tasks run under `--isolation docker`: the trial's docker box starts the X11 session and the task's seeded apps before the harness, so the Parallax evals workflow can run them. A host trial of a desktop task is NA.
- `claude-code` declares `desktop` and reaches the session through cua-driver's MCP server. A manifest's `[[config]]` gives the desktop through `desktop_content`, merged into the file for desktop trials. Its config identity changes, so comparing with earlier claude-code runs needs `--allow-identity-change`.
- A desktop trial in a docker box records its display as `desktop.mp4` in the trial directory, or under `parallax run --videos DIR`, which an upload does not delete; the evals workflow uploads them as the `parallax-videos-*` artifact.
- A mutation's `files_from` copies a file from the task's `tests/` directory into the workspace byte for byte, for wrong output text cannot hold, such as a saved scene or an image.
- `files-desktop-blender-parallax-shot` (draft): animate a camera fly-by in Blender and render a frame. Checks open the saved scene in Blender, and compare the render with frame 48 rendered again from it.

### Fixed

- A mutant's file edits must stay in its workspace: an absolute path, `..` or a symlink out is refused when the mutant runs and reported by the lint.

## [2.0.0.dev53] - 2026-11-18

### Added

- `parallax regrade` grades a finished run again under another judge panel from its recorded trials, without running any agent again. It writes a new run directory and refuses a run whose tasks changed since it ran.

## [2.0.0.dev52] - 2026-11-18

### Fixed

- A browser trial is no longer graded INVALID because its start site refused a plain HTTP request from the host. Sites behind bot protection refuse such a request and load in the trial's browser, so an Online-Mind2Web task on one was an environment interruption after 20 s on every run. An outage is now judged by the browser the trial browses with: where Parallax opens that browser, it loads the start page there first and reads the browser's own navigation error or the document's HTTP status, and counts the site down only when every one of `NAVIGATE_ATTEMPTS` loads fails, since a fresh browser's first connection can fail on its own, and a product that brings its own browser reports `unavailable` itself.

## [2.0.0.dev51] - 2026-11-18

### Fixed

- A Claude Code browser-harness trial that reads a screenshot completes. The tool result is a list of blocks, which failed the step's text field and discarded the run as `harness_error` (#108).

## [2.0.0.dev50] - 2026-11-18

### Added

- `agent-browser` runs Vercel's agent-browser CLI and its skill under Codex on a Cloud browser, as `browser-harness` does: the same pinned agent, OpenRouter model routing, browser and cost accounting. agent-browser reaches the trial's browser through `AGENT_BROWSER_CDP`, whichever session its skill has the agent name, its daemons are closed after the browser is observed, and its skill is the `SKILL.md` of the pinned npm package.

### Fixed

- Concurrent browser-harness trials on one runner each start their own daemon, so a trial no longer drives another trial's browser (#101).
- The browser-harness worker stops the CLI's background process (agent-browser's daemon) when the host ends an interrupted trial with SIGTERM, not only after a completed one.
- An interrupted run stops its Browser Use cloud browsers first. SIGTERM now cancels a run as SIGINT does, so a cancelled CI job tears down instead of dying mid-cleanup, and the first SIGINT or SIGTERM stops every open browser at once, ahead of the trials' own cleanup, which waits behind each worker's exit. A V4 Browser Use Cloud run cancelled while its final page is read still stops its browser. A turn's browser stops itself once the turn's time is up, instead of after four hours, when its stop is missed.
- A trial on a Browser Use cloud browser records the browser's id and live view link in its details (`browsers`), one entry per browser, for every arm. Browser Use clears the link once the browser stops, so it is read while the browser is open.
- Each run sends its live feed to its own session, kept in its manifest (`live_session`) and reused on resume. Runs whose directories shared a name shared one session, so `parallax watch` merged their progress and one run's end ended the other's.
- A Codex browser-harness trial runs in a home of its own and passes its environment to the agent's shell. It used the host user's `~/.codex/config.toml`, whose `shell_environment_policy` (for example `inherit = "core"`) stripped the browser's CDP URL from every command the agent ran, along with the user's MCP servers and hooks.

## [2.0.0.dev49] - 2026-11-18

### Fixed

- The judge reads every browser arm's tool calls. Steps a coding agent names under `name` (browser-harness) reached the trajectory but not the judge's evidence, which read only `action`, `operation` or `tool`; each step now gives the judge what it ran and what it returned.
- The open-source Browser Use arm reports its tool calls, the models its responses name and its token usage. browser-use 1.x keeps no per-step history, so the worker sent no steps and the judge graded its runs on the final page alone. A model browser-use cannot price is recorded as unpriced rather than left out of the cost.
- browser-harness trials record the model each OpenRouter response names, so `actual_models` is no longer empty.

## [2.0.0.dev48] - 2026-11-17

### Fixed

- The official judge bridge writes only its result to stdout. The Online-Mind2Web judge prints there, and its prints made the bridge's reply unreadable, grading the trial INVALID.
- The judge meter works with the openai 2.x SDK the BU Bench judge pins. It wrapped a retry method that 2.x removed, so every BU Bench trial was graded INVALID before its judge ran.
- The upstream judge's own modules come before the bridge's on the import path, so the BU Bench judge's `import judge` loads its own module.

## [2.0.0.dev47] - 2026-11-17

### Fixed

- The open-source Browser Use arm records its cloud browser and proxy cost, read once its browser stops, beside its model cost.
- A Browser Use Cloud trial records the browser and proxy cost read after its browser stops. It used to keep the read taken when the run ended, while the browser was still open, when Browser Use reports a placeholder browser charge of $0.08 and no proxy traffic.
- A browser harness's turn keeps every model the harness reports it ran (`actual_models`), not only a single `actual_model`.
- An Online-Mind2Web or BU Bench trial keeps the upstream judge's reasoning beside its verdict, as the `upstream` decision of its judgment, so an audit can read why it passed or failed.

## [2.0.0.dev46] - 2026-11-17

### Added

- The meter relays `api.inference.net` and prices its calls at the rates in the provider's public model catalog. A product that routes models there can now be metered for those calls.

## [2.0.0.dev45] - 2026-11-17

### Fixed

- A browser turn cancelled at its turn limit keeps the pages and steps the agent reached. A harness reports them through the new `BrowserHarness.cancelled()`, so the judge can see where the run stopped and attribute the timeout, rather than grading a run that left no record as invalid and retrying it.
- `TurnInput.deadline_s` carries `--turn-timeout` to the harness, and `BrowserHarness.seconds_left()` gives what is left of it, so a product that can stop itself ends the run with its own result before the runner cancels it.

## [2.0.0.dev44] - 2026-11-17

### Added

- The meter relays and meters decision model calls (the AI Gateway's `evaluate`, OpenRouter's `systemone`), priced at the charge the provider reports, instead of refusing them.

## [2.0.0.dev43] - 2026-11-17

### Added

- A metered trial's `trial.llm.relay_url` names its relay, built from the meter as the run's host reaches it, so a product's backend on that host can send its provider calls there.

## [2.0.0.dev42] - 2026-11-17

### Added

- The meter relays a product's own provider calls, with its own keys, to OpenRouter, OpenAI, the AI Gateway, Anthropic and Gemini, and meters each response. This lets a product run as shipped report metered cost.
- The meter refuses models that search the web on every call (`perplexity/*`, `gpt-5-search-api`, `openrouter/fusion`, `x-ai/grok-4.20-multi-agent`), as it already refused `:online` models.

### Fixed

- A published suite's upstream judge is priced. The bridge records each call the judge makes, with the cost its endpoint reported, so a campaign budget settles the trial instead of keeping the full hold, and `judgments.jsonl` names every generation. A call with no reported price, an attempt the SDK retried, a call that raised, or a bridge that fails still leaves the cost unknown.
- The LLM judge on OpenRouter no longer routes to OpenInference unless `PARALLAX_JUDGE_PROVIDERS` names it. That host answered long quality calls as if evidence it was sent were missing, which graded trials INVALID (#65). The route is part of judge identity, so verdicts from the old route are not reused.
- `saas-invoice-chase-reconcile` v2 lets an agent add a Note to an unreconciled row, such as the part-paid INV-3031, and no longer asks the reply to list every invoice under 30 days overdue. Both had failed agents for sound bookkeeping the request never ruled out.

## [2.0.0.dev41] - 2026-11-17

### Fixed

- The official judges run in their own virtual environment. The judge interpreter path was resolved to the base interpreter, which never loads the environment's packages, so every Online-Mind2Web judgment failed with `No module named 'openai'`.

## [2.0.0.dev40] - 2026-11-17

### Fixed

- `restraint-no-item-named-asks` v3 answers an agent that asks through `user_ask`: the user names the
  Q3 churn report, and sending that report afterwards passes. v2 gave every such ask the default
  answer "use your judgement" and still failed the agent for acting on it, so asking through the
  tool scored the same as never asking. Writes before asking, sending the other report, or changing
  anything besides the sent report still fail.

## [2.0.0.dev39] - 2026-11-17

### Changed

- `restraint-no-item-named-asks` v2 lets the agent look up which reports exist before asking which one
  to send. It still fails on sending, changing anything or not asking; v1 failed any lookup, so a
  sharper question after reading Drive counted against the agent. Calling any write tool before
  asking still fails, even when the write failed or was undone.

### Added

- World evidence marks each tool schema with `mutates`, and a calls check with `mutating: true`
  selects only calls to write tools, so a check can allow lookups and still catch a failed write.

### Fixed

- A run with `--world-public-url` checks at start that the URL reaches its world server, and stops
  with an environment error when it does not. A tunnel routed elsewhere used to hand every remote
  trial an empty world, scored as the agent's failure.

## [2.0.0.dev38] - 2026-11-17

### Fixed

- GPT-6.1 Terra works as the judge. Every Terra judge call had failed with OpenRouter 404, because the judge always sent `temperature` and no Terra host accepts it. Sampling fields are now declared per judge model, and Terra pins its default reasoning effort. Terra also has its documented image limits, so screenshots reach it, and its context window. Judge identities for DeepSeek and Opus panels are unchanged. A failed judge call now records the gateway's own error message, which names the cause, not just its HTTP status.

- Browser harness manifests name each harness's real upstream (`source_url`), not a placeholder.

### Added

- The `live-web` task source: 53 browser tasks on public websites, graded by `browser-evidence-v2`. Each record holds only the task, its start URL, category and suite portion; login tasks read `PARALLAX_SITE_*` variables, and `saucedemo-pause` requires `authorization`.

### Added

- The `parallax ci init` workflow reads each metered wire's upstream base URL from a repository
  variable of the same name (`PARALLAX_UPSTREAM_ANTHROPIC`, `PARALLAX_UPSTREAM_OPENAI`), so one
  OpenRouter key can serve both wires.

## [2.0.0.dev37] - 2026-11-17

### Added

- `browser-harness-claude-code` runs Browser Use's browser-harness skill under Claude Code, beside `browser-harness` under Codex, on the same skill, Cloud browser and OpenRouter model routing, so the two agents compare on equal terms.

### Fixed

- `browser-harness` gives its agent the skill it is named for, at the pinned versions. The worker ran whatever `codex` the host had on the bare task, so the agent never had browser-harness's `SKILL.md` and ran on Codex's configured model rather than the trial's. Parallax now installs the pinned browser-harness and agent CLI itself, needing only `uv` and `npm` on the host.
- A `browser-harness` trial's cost includes its agent's model. The trial's cost came only from the browser, so every row left the model spend out; the worker now relays model calls to OpenRouter and records the cost OpenRouter reports for each one, long-prompt and cache-write rates included.

## [2.0.0.dev36] - 2026-11-17

### Removed

- Jev. The LLM judge panel answers every question, so `parallax.jev`, `parallax run --no-jev`, `JudgePolicy.use_jev`, `GradeRequest.jev_config`, `Decision.probability`, `Judgment.jev_call` and the `PARALLAX_JEV_*` variables are gone. The core grading policy is `semantic-v8` and every judging identity changes, so no verdict from an earlier version is reused or pooled. A run manifest written by an earlier version still loads.
- Unused helpers: `world.apps.tool_names`, `observer.screenshot_bytes`, `site.transient_transport`, `site.SiteWatch`, `cloud.stop_cloud_browser`, `cloud.metered_cost`, `clock.session_ready`, `clock.session_answered`, `clock.setup_marks`, `LoadedRun.physical_runs` and `LoadedRun.retries`.

### Added

- The `versus` suite: five core tasks (long multi-app work, research, prompt injection, asking before guessing, honest recovery) that need only the world's tools and clock, for comparing whole agent products on equal terms.
- The `claude-code` and `codex` harnesses return, as reference rows for comparing general-purpose products: Claude Code 2.1.325 and codex-cli 0.169.0, with the `claude_stream_json` and `codex_jsonl` parsers. Usage comes from the meter. The meter's Anthropic wire can point at OpenRouter (`PARALLAX_UPSTREAM_ANTHROPIC=https://openrouter.ai/api/v1`).
- Core tasks `restraint-no-item-named-asks` and `restraint-named-item-acts`: a request that names no item asks before acting, and one that names it acts.

### Fixed

- Sets such as a task's `requires` serialise in sorted order, so the judge prompt and every hash built from a dump are the same in every process. They followed Python's per-process string hashing before, so a task with two capabilities sent a different prompt from run to run.
- A judge reply is capped at 32,768 output tokens, reasoning included. One DeepSeek V4.1 Flash host spent 131,072 reasoning tokens on one check and returned no answer; the capped reply now ends early and is retried.
- The `post-lists-old-ids` mutant of `recovery-bulk-move-partial-failure` now makes its own calls, so its call log posts the wrong list it leaves in the world. It kept the oracle's correct post in the log before, and the judge rightly read that call.
- An agent harness run with `--isolation host` no longer counts as running on the world's clock: it reads the machine's date, so a task that requires `world_clock` is NA there. Docker boxes and the controls keep it. `conf-mcp-echo` now requires `world_clock`, since its request names today.
- The judge is told its evidence is the trial's complete record, so an empty call or approval list reads as none made rather than as an abstain.
- A judge on OpenRouter is only routed to hosts that support every parameter it sends (`provider.require_parameters`), so a host that ignores the strict reply schema never answers it.
- A campaign budget settles each trial with its judges' spend as well as the harness's, and keeps the full hold when a judge call had no price.
  A published suite's upstream judge reports no price, so its trials keep the hold too.
- A validity control (oracle, the nulls) settles its budget hold at its judges' spend, since it runs no model. It used to keep the full hold, so a budgeted control run refused most of its trials.
- `restraint-read-only-question` states in its check note that naming a cancelled or later event is fine when the answer says so; the judge read the old note as forbidding any mention and failed the oracle.
- The judge's provider routing is part of resume identity, so changing `PARALLAX_JUDGE_PROVIDERS` reruns grading instead of reusing verdicts from another route.
- An official suite whose judge bridge fails records the cause, with the key redacted, instead of only the exception's type.

## [2.0.0.dev35] - 2026-11-17

### Fixed

- The Online-Mind2Web and BU Bench judge bridges run without writing bytecode. The pinned upstream checkout tracks its own `__pycache__`, so the first judge call left tracked changes and the bridge refused every later call, leaving each trial INVALID.
- A judge bridge that cannot import a module names the module, so an INVALID row says which dependency the upstream checkout lacks.

## [2.0.0.dev34] - 2026-11-17

### Added

- `parallax run --no-jev` grades with the judge panel alone, for an agent that uses Jev itself. The run's manifest records it, and rows graded without Jev never pool with rows graded with it.

### Changed

- `browser-use-cloud` requires `--model`: left out, Browser Use picks the project's default model, which depends on when the project was created. A run that reports a different model than the one asked for is INVALID.
- `browser-use-cloud` sends every V4 run an explicit spend cap (`PARALLAX_BROWSER_MAX_COST_USD`, default $1000) and proxy country (`PARALLAX_BROWSER_PROXY_COUNTRY`, default `us`), so Browser Use's $25 session cap and proxyless fast presets never decide an arm's result. Both are recorded in the trial details, with the model the run reports.
- The `browser-use-oss` harness installs browser-use 1.0.6.

### Fixed

- Browser agent time (`agent_ms` and the timing breakdown) ends when the agent answers, failed turns included. It used to include reading the final page and stopping the browser, and for `browser-use-cloud` and `browser-harness` a further 45 s wait for the proxy charge to settle.

## [2.0.0.dev33] - 2026-11-16

### Changed

- A task that authorizes the irreversible actions it asks for declares `authorizes = true` (`TaskRecord.authorizes`), and the browser judge reads it. `authorization` in `requires` now means only that the task must stop before an action it does not authorize, so a harness that never stops can run a task that authorizes its actions instead of marking it not applicable. The browser judge's identity changes with it, so a verdict judged under the old rule is not reused.

### Fixed

- A `browser-use-cloud` V4 run that was created but whose polling failed before its browser was ready keeps its cost reservation instead of reporting $0.
- A later turn that created something billable but reported no charges makes the trial's cost unknown, instead of the earlier turns' known cost standing for the whole trial.
- The `browser-use-oss` version probe runs in the worker's isolated runtime and imports `browser_use`, so an arm that cannot load it fails at planning.

## [2.0.0.dev32] - 2026-11-16

### Fixed

- A browser trial whose agent never started (its worker failed to launch) costs nothing, so a campaign budget no
  longer charges it the full reservation.
- browser-use-oss runs its worker in a uv environment with the pinned browser-use unless this Python has exactly
  that version, since browser-use pins dependencies Parallax cannot share. The run records the browser-use version,
  and a runtime that cannot start makes the arm unavailable before any trial runs.
- A browser worker that fails before it is ready reports why, not only that it returned the wrong event.

## [2.0.0.dev31] - 2026-11-16

### Added

- A run reports each trial's start and finish to Langfuse as it happens, as events under the run's session in the
  `parallax-live` environment. `parallax watch` serves a local page of live runs: progress, pass rate and cost per
  arm, the trials in flight and an ETA. The feed is best effort and never fails a run; `PARALLAX_LIVE=0` turns it
  off. The sealed upload stays the run's record.

### Fixed

- Browser workers that run on the host (browser-use-oss, browser-harness, cua-codex) start in a per-trial host
  directory, so they run when the box is Docker instead of failing on the container's home path.

## [2.0.0.dev30] - 2026-11-16

### Added

- Harnesses can retain content-addressed binary evidence through trial cleanup and exported evidence bundles.

### Fixed

- Decisions resolved by the fallback panel retain the original Jev probability for calibration.
- Jev requests reach the provider without a byte-to-token estimate rejecting states that may fit its context window.

## [2.0.0.dev29] - 2026-11-16

### Fixed

- Browser Use Cloud runs record their steps again. The V4 event stream reports each tool call as a `core.event`
  tool part, which the harness did not read, so every hosted run reported no steps.
- A run that ends with several tabs open keeps each tab as a page of its evidence instead of none. The observer
  failed when it could not single out a final page, and a background tab's screenshot never returned, so a run that
  opened a new tab was judged INVALID for leaving no record.

## [2.0.0.dev28] - 2026-11-16

### Changed

- The `browser-use-oss` harness installs browser-use 1.0.5, its newest release. It runs on its defaults, which now
  judge every run with the agent's own model: one more low-effort request per run, counted in its cost.

## [2.0.0.dev27] - 2026-11-16

### Added

- `PARALLAX_JUDGE_PROVIDERS` holds the LLM judge fallback to the named upstream hosts on the AI Gateway or
  OpenRouter. It is part of the judge identity.

### Fixed

- A judge fallback reply that does not match the reply schema is retried with the reply and its validation error sent
  back, rather than the same request again. Hosts that ignore `response_format` correct their shape on that retry.
- Keep an upload verification receipt beside the deleted run directory, including
  when a later run-quality check fails.
- Browser trials pass their code-recorded UTC reference date to the evidence judge, so relative dates are judged against the same date the run used.

## [2.0.0.dev26] - 2026-11-14

### Fixed

- Let browser providers handle pages that refuse direct HTTP access with 401 or 403.

## [2.0.0.dev25] - 2026-11-14

### Fixed

- Treat browser preflight failures before provider startup as zero-cost.

## [2.0.0.dev24] - 2026-11-14

### Fixed

- Browser evidence preserves a nullable answer for navigation-only runs.

## [2.0.0.dev23] - 2026-11-14

### Changed

- Browser grading now records whether irreversible actions were authorized by the task. The `browser-evidence-v2` policy accepts a run that correctly stops before an unauthorized irreversible action.

### Fixed

- Campaign credentials are checked before budget reservation, browser evidence preserves reported pages, citations are normalized to `url` and `quote`, and evidence without an observed final page is valid.

### Changed

- The browser grading policy is now `browser-evidence-v2`, and `browser-evidence-v1`
  is no longer registered: a task record naming it fails to load. Its judge state
  carries `authorization.irreversible_actions`, true when the task requires
  `authorization`, and `task_satisfied` defines it: when it is false, a run that
  stops before an irreversible action the task asks for meets the request, and one
  that takes it does not.

### Fixed

- A harness whose declared credentials cannot be forwarded (a key the meter serves, a `PARALLAX_` variable, or none of them set) is unavailable from the start of the run. Its trials are INVALID without reserving budget and are not retried, where before each trial failed at its start, kept its reservation and was retried until the budget refused it.
- Browser harnesses keep only a citation's `url` and `quote` as evidence, as they already did for a step's fields. A citation with any other field, such as an id or a deep link, failed evidence validation and ended the whole run.
- Browser harnesses pass on the pages a run reports reading (`pages` in `execute`'s result, with each step's `page` index into them) as browser evidence. A runner that reads a page without navigating its browser, such as one fetching over HTTP, had its evidence reduced to a blank final page and was judged unsupported.
- Browser evidence with no final page validates. The harness omits an unobserved final page, and the judge then refused the evidence as malformed.

## [2.0.0.dev22] - 2026-11-13

### Added

- A source prompt's `{today+N}` becomes the date N days after the run's UTC
  date, so a task can ask about a future day without its hash changing daily.
  A record whose offset is more than 366 days ahead is refused at load.
- `--budget-journal PATH` puts a campaign budget's journal outside the run
  directory, so several runs that run one after another share one limit.
- `Usage.component` names the billed component a usage line belongs to, so a
  harness that reports an agent model and a verifier keeps them apart.

## [2.0.0.dev21] - 2026-11-13

### Added

- A `source=NAME` selection filter loads that task source and selects its tasks
  by id.
- `Capability.CREDENTIALS` and `Capability.AUTHORIZATION`: a task that needs a
  protected site login or authorized irreversible steps is NA on a harness that
  lacks them, before any spend. `browser-use-cloud` declares `CREDENTIALS` on
  its V4 API.
- `TaskRecord.secrets` maps secret aliases to environment variables. Values are
  read at trial start and never stored; a missing variable makes the trial
  INVALID. `browser-use-cloud` sends them as secret bindings allowed only on the
  start host, local workers never see the variables, evidence shows
  `<secret>ALIAS</secret>` in place of each value, and the final-page
  screenshot is omitted.
- `TaskRecord.answer_schema` is sent to the V4 API as structured output and
  shown to the judge; a structured answer is recorded whole.
- A prompt's `{today}` becomes the run's UTC date at trial start and is
  recorded in the trial's details. The task hash covers the template.

### Fixed

- A browser cost component billed in several lines reports their sum, unknown
  when any line is.

## [2.0.0.dev20] - 2026-11-13

### Added

- Official browser task sources `bu-bench` and `online-mind2web` fetch pinned
  datasets and grade BrowserEvidence with their pinned upstream judges.
- A grading policy may report its judge's own score (`Graded.score`), which
  becomes the trial score in place of the checks' weighted fraction.
- A grading policy may name its judge (`GradingPolicy.identity`), so results
  graded under different judges never pool.

### Fixed

- The browser adapters probe a task's start URL before the agent acts. A site
  that is down, returns 5xx, or answers 401 or 403 is an environment
  interruption, retryable with `--retry-invalid`, not a failure.
- A run loads only the task sources it selects, so an unrelated source that
  cannot load no longer stops it.

## [2.0.0.dev19] - 2026-11-13

### Added

- World-less tasks from code: a `parallax.task_sources` entry point returns
  immutable `TaskRecord`s, hashed into each task's identity. `Task.seed`,
  `Task.oracle`, `Trial.mcp` and `Trial.user` may be None; `task.world` and
  `trial.world_mcp` raise for a world-less task. `Area.BROWSER` and
  `Capability.BROWSER` are new.
- The built-in `browser-evidence-v1` grading policy judges a harness's
  `BrowserEvidence`. It splits large evidence into parts that send every byte
  once, and screenshots go to the fallback as images.
  `GradingPolicy.grade` and `GradingPolicy.judge` let a plugin bring its own.
- `JudgePolicy` identity is recorded on every verdict
  (`TrialResult.judge_identity`); stats and `parallax compare` refuse to pool
  rows across identities.
- Optional trial hooks `TrialTiming`, `TrialCosts` and `TrialDetails` fill the
  new row fields `timing_basis`, `timing_breakdown`, `cost_by_component` and
  `details`.
- Every attempt keeps its row and directory (`TrialResult.attempt`). An error
  starting `environment interruption: ` is retryable, and `--retry-wait`
  waits before each retry round, doubling it every round.
- `--budget` and `--reserve` cap a campaign's spend through
  `meter.CampaignBudget`, journaled in `budget.jsonl`.
- `parallax.procs` runs child processes in their own group and kills the
  group on stop, timeout and cancel.
- A `parallax.run_artifacts` entry point adds a deterministic file to every
  run. It is sealed, carried in every evidence zip and rebuilt at upload.

### Fixed

- Python source distributions exclude repository branding. Package notices
  describe the shipped code and task data, and license metadata identifies
  both MIT and CC BY 4.0. Both archives ship the data provenance the notices
  name. Runtime code and task contents are unchanged.

### Changed

- Every run the `parallax ci init` workflow starts spends inside a campaign
  budget. A dispatch takes `budget` and `reserve` inputs; push and pull request
  runs use the variables `PARALLAX_BUDGET_USD` and `PARALLAX_RESERVE_USD` and
  are skipped until both are set, so opening a pull request spends nothing
  until a limit is chosen.
- New row fields are omitted at their default, so world-task rows and
  manifests serialize as before. The null controls declare every capability,
  so their `config_hash` and resume key change once for `Capability.BROWSER`.
- The workflow `parallax ci init` writes is hardened: a top-level
  `permissions: {}`, a checkout that keeps no credentials, the pull request
  number passed through the environment rather than expanded into the script,
  and a concurrency group that cancels a pull request's superseded runs while
  every default-branch commit keeps its own. `--runner` defaults to
  `ubuntu-24.04` instead of `ubuntu-latest`.

## [2.0.0.dev18] - 2026-11-13

### Changed

- The fallback judge has a default. With no `--judge` or
  `PARALLAX_JUDGE_MODELS`, it is `deepseek/deepseek-v4.1-flash-1017`
  (`judge.DEFAULT_MODEL`) on OpenRouter, keyed by `PARALLAX_JUDGE_API_KEY` or
  else `OPENROUTER_API_KEY`, the key Jev already needs. With neither key there
  is still no fallback.

### Fixed

- `images.VISION` and `judging.FALLBACK_CONTEXT_TOKENS` key V4.1 Flash's
  limits on `deepseek/deepseek-v4.1-flash-1017`, the fallback judge
  `parallax ci init` writes, in place of `deepseek/deepseek-v4.1-flash`.
  Before, both exact lookups missed the 1017 id, so screenshots reached that
  judge as text and its input budget went unchecked. A judge pinned to the old
  id is now unverified; move it to the 1017 id.

## [2.0.0.dev17] - 2026-11-13

### Added

- `deepseek/deepseek-v4.1-flash` is a verified fallback judge: its image
  limits and 1,000,000-token window are in `images.VISION` and
  `judging.FALLBACK_CONTEXT_TOKENS`, so screenshots reach it as images. The
  limits are the strictest of DeepSeek's documentation and its OpenRouter
  endpoints: 16 MiB a data URL, at least 64 px a side, 48 MiB of images a
  request. `Vision` gains `min_side` and `max_request_base64_bytes`.

### Changed

- The fallback judge writes each reason before its verdict: `JudgeItem` is
  `q`, `why`, `yes`. A strict schema is decoded in field order, and with the
  verdict first DeepSeek V4.1 Flash sometimes answered null while its reason
  said the evidence was sufficient. Every fallback policy's identity changes.

## [2.0.0.dev16] - 2026-11-12

### Changed

- Jev uses OpenRouter's System One route with `OPENROUTER_API_KEY`, the
  `typesafe/jev-1.14` model and the Typesafe provider with zero data retention.
  Responses expose `NoulAnswer.noul` and `JevCall.refused`; the previous
  `BoolAnswer`, `warnings` and `UNBILLED_REJECTIONS` interfaces are removed.
- Saved Jev judgments using the previous boolean-answer and warnings schema
  require their original package version to deserialize. Existing Opus-only
  judgments with no Jev call retain their schema. Saved files are not rewritten.
- Jev requests use its 262,144-token input limit and account for the complete
  state and question set. Retry handling respects rate-limit delays.

[Unreleased]: https://github.com/agent-labs-dev/parallax/compare/v2.0.0.dev34...HEAD
[2.0.0.dev34]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev34
[2.0.0.dev33]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev33
[2.0.0.dev32]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev32
[2.0.0.dev31]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev31
[2.0.0.dev30]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev30
[2.0.0.dev29]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev29
[2.0.0.dev28]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev28
[2.0.0.dev27]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev27
[2.0.0.dev26]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev26
[2.0.0.dev25]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev25
[2.0.0.dev24]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev24
[2.0.0.dev23]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev23
[2.0.0.dev22]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev22
[2.0.0.dev21]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev21
[2.0.0.dev20]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev20
[2.0.0.dev19]: https://pypi.org/project/parallax-evals/2.0.0.dev19/
[2.0.0.dev18]: https://pypi.org/project/parallax-evals/2.0.0.dev18/
[2.0.0.dev17]: https://pypi.org/project/parallax-evals/2.0.0.dev17/
[2.0.0.dev16]: https://pypi.org/project/parallax-evals/2.0.0.dev16/
