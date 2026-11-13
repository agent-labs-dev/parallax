# Changelog

All notable changes to Parallax are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[PEP 440](https://peps.python.org/pep-0440/).

## [Unreleased]

## [2.0.0.dev22] - 2026-11-13

### Added

- A source prompt's `{today+N}` becomes the date N days after the run's UTC
  date, so a task can ask about a future day without its hash changing daily.

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

[Unreleased]: https://github.com/agent-labs-dev/parallax/compare/v2.0.0.dev22...HEAD
[2.0.0.dev22]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev22
[2.0.0.dev21]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev21
[2.0.0.dev20]: https://github.com/agent-labs-dev/parallax/releases/tag/v2.0.0.dev20
[2.0.0.dev19]: https://pypi.org/project/parallax-evals/2.0.0.dev19/
[2.0.0.dev18]: https://pypi.org/project/parallax-evals/2.0.0.dev18/
[2.0.0.dev17]: https://pypi.org/project/parallax-evals/2.0.0.dev17/
[2.0.0.dev16]: https://pypi.org/project/parallax-evals/2.0.0.dev16/
