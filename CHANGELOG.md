# Changelog

All notable changes to Parallax are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased]

## [2.0.0.dev35] - 2026-11-17

### Fixed

- The Online-Mind2Web and BU Bench judge bridges run without writing bytecode. The pinned upstream checkout tracks its own `__pycache__`, so the first judge call left tracked changes and the bridge refused every later call, leaving each trial INVALID.

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
  Publication follows [the release procedure](docs/release-dev16.md) with
  this version and tag `v2.0.0.dev17`.

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

The release procedure is in [the dev16 release plan](docs/release-dev16.md).

[Unreleased]: https://github.com/agent-labs-dev/parallax/compare/v2.0.0.dev27...HEAD
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
