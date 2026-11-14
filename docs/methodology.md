# Methodology

Parallax measures **harness plus model, end to end**. It asks whether the agent
product left a seeded set of services in the state the user asked for. It checks
that the agent did so without collateral damage, without claiming work it did not
do, and without acting where it should have asked. This page defines how a trial
becomes a verdict and how verdicts become a harness/model row. It also records
which validity threats the design addresses and which it does not yet address.

- [Evidence](#evidence)
- [Verdicts](#verdicts)
- [Controls](#controls)
- [Statistics](#statistics)
- [Uncertainty](#uncertainty)
- [Observed models](#observed-models)
- [Cost](#cost)
- [Web isolation](#web-isolation)
- [Rubrics and judges](#rubrics-and-judges)
- [Contamination](#contamination)
- [Maturity lifecycle](#maturity-lifecycle)
- [Reproducibility](#reproducibility)
- [ABC checklist self-assessment](#abc-checklist-self-assessment)

## Evidence

A check may read only evidence that Parallax owns:

- the world's final state and its diff against the seed;
- the world call log (`world_log.jsonl`), written by the MCP server, not by the
  harness;
- the `/workspace` snapshot taken after the harness stops;
- a trusted verify run: a fresh `--network none` container over the snapshot,
  never the agent's own box;
- the per-turn answers the runner captured.

The harness's own trace (steps, tool calls, claimed cost) is stored for
diagnosis, but it is never graded. Every harness is
therefore graded on the same evidence, whatever it chooses to report about itself.

## Verdicts

A trial ends in one verdict. When more than one condition holds, the first row
in the table below wins: NA > INVALID > FAIL > PASS.

| Condition | Verdict |
|---|---|
| The task requires a capability the trial does not have (see [Capabilities](#capabilities)) | **NA**. The trial is never run and does not count against the harness |
| World `internal_error`, missing evidence, boot error, meter unreachable, or execution the semantic validity judge rejects or cannot attribute | **INVALID**. Excluded, counted and rerun on resume. A timeout/harness status alone cannot assign agent failure |
| An INVALID trial whose reason is a transient execution fault of that attempt (boot error, timeout, budget or harness error, world registration/control/evidence/clock fault, meter unreachable) | **INVALID**, then re-run inside the same run. `--retry-invalid N` (default 1) allows N further attempts; the earlier attempt stays in `results.jsonl` and is never scored |
| Otherwise | **PASS** if every non-advisory gate check passed, otherwise **FAIL** |

Each trial also records a `score`, the weighted fraction of non-advisory checks
passed. This is reported as `partial`. A task with a known upstream defect
carries `xfail` and is scheduled only with `--include-xfail`. A run whose
INVALID share exceeds 10% exits non-zero, and `parallax compare` never pairs it.

### Capabilities

A task lists the capabilities it needs in `requires`. A trial has the
harness's declared capabilities, and the runner adds one of its own:

- **`world_clock`** means the agent runs on the world's clock, so "today",
  "tomorrow" and "next week" mean the scenario's dates. The runner grants it to
  every harness whose process it owns: a docker box runs under the world's
  clock, and host isolation is for controls and development. A remote product
  keeps its own clock and has `world_clock` only if it declares it.

A task requires `world_clock` when an agent that believed it was a different
day would be graded differently for otherwise-correct behaviour: relative dates
("next week", "overdue", "this month"), a one-off time such as "Friday at
4pm", a scheduled run that must pick out that day's items, or a stated deadline
that would look already past. A recurring schedule that names no date does not
need it. A trial missing any required capability
is NA with the reason `requires <capabilities>`.

## Controls

Every task passes four controls before it ships, and CI checks them again on
every change (see [authoring.md](authoring.md#controls-and-the-negative-twin-rule)):

1. The oracle scores 1.
2. `null-claim` scores 0.
3. `null-refuse` scores 0.
4. Every mutant is killed.

`parallax check SELECT` runs them. The controls are also ordinary harnesses
(`oracle`, `null-claim`, `null-refuse`), so a run can include them as rows of
their own; such rows are marked as validity controls in Langfuse.

## Statistics

Statistics are computed per (harness, model) row over valid trials and recorded
as experiment scores in Langfuse (see [tracking](tracking.md#what-is-recorded)):

- **pass@1** is the mean over tasks of each task's mean verdict across repeats.
  Every task has the same weight, however many repeats it has.
- **pass^k** is the unbiased estimate of the chance that all k repeats pass. For
  each task it is C(c,k)/C(n,k), with n valid repeats and c passes. Tasks with
  n < k are excluded. pass^k is the reliability number, and it falls quickly
  for agents that pass only intermittently.
- **partial** is the mean `score`.
- **Cost** is the mean cost per task (see [Cost](#cost)).
- **Agent time** is the agent's own time: for each turn, from submitting the
  prompt to the final reply, summed over the trial's turns. Each trial also
  records time to first token, boot time and trial wall time. Trial wall
  includes setup, teardown and evidence collection, so it is not comparable
  across isolation modes.
- **Coverage** is the count of valid and INVALID trials.

## Uncertainty

- **Confidence intervals** come from a task-clustered bootstrap, with B = 10,000
  and seed 0. Tasks are resampled and each task keeps all of its repeats, so
  repeats never pass for independent evidence.
- **Paired deltas** (`parallax compare`) are computed on the task intersection
  with the same bootstrap. A delta whose CI contains 0 is reported as tied,
  never as a win. Runs that measured different things are not paired: see
  [versioning](tracking.md#versioning). The harness revision and the served
  models are the treatment, so they never block a pair.
- **Power.** With the 20-task `lite` suite, only large differences are
  detectable; repeats help less than tasks, because repeats of one task are
  correlated. A difference whose interval spans 0 is noise, however the
  numbers look.
- **Lite.** Lite is a hand-stratified 20-task subset for cheap iteration. It is
  smoke only, not ranking-validated, until its rankings agree with Core's at
  Kendall's τ ≥ 0.8.

## Observed models

The meter records the model that served every request, and the requested model
is never taken as evidence of it. A trial whose responses did not name their
model carries a `model_unobserved` flag, and one whose responses named a model
other than the one requested carries `model_mismatch`; the flags are recorded
with each repeat. A row of the same model
across harnesses compares harnesses; a row where the product chose, routed or
substituted the model compares products as shipped.

## Cost

| Source | Meaning |
|---|---|
| metered (M) | A per-trial proxy saw every request. Tokens are priced with `genai-prices`, and the observed model is recorded |
| harness (H) | The product's own usage report. It is shown, but it is not comparable to metered cost |
| none (`-`) | Unknown. Never rendered as $0 |

An unknown price is recorded as missing, never as zero. Parallax caps neither
cost nor time: the meter records every request and refuses none for spend, and
a turn runs until the product ends it, under the product's own limits.

## Web isolation

Every row records `web_isolation`:

| Value | Meaning |
|---|---|
| `enforced` | Docker isolation. The box can reach only the world and meter sidecar, so native web tools cannot fetch live data |
| `flags` | Host isolation. Native web tools are turned off by flag or config only. For development |
| `none` | Remote. The product runs outside Parallax's control |

Research tasks are graded against the seeded `web` service. A row that is not
`enforced` could have read the live web, and its `web_isolation` says so.

The meter closes the path out of the box through the provider's tools: a
generation request that asks the provider to run a tool. Provider-side tools (Anthropic web search, web
fetch, code execution and `mcp_servers`; OpenAI hosted web search, file search,
code interpreter, MCP and image generation; OpenRouter `plugins`,
`web_search_options` and `:online` models) are refused with a 403 in the
provider's error shape that names the tool. Only tools the harness executes
itself pass: untyped or `custom` tools, `bash` and `text_editor` on Anthropic,
and `function`, `custom`, `local_shell` and `namespace` groups of those on
OpenAI. A request body the meter cannot read is refused too, and the meter
forwards the body it checked rather than the raw bytes. Nothing is stripped
silently. Hermes and OpenClaw, measured with their Parallax
configuration, send no provider-side tool, so the refusal does not fire on
their normal runs. It applies under every isolation mode. The meter does not
refuse a model that searches the web without a tool, such as Perplexity Sonar
or OpenAI's search models; such a model shows in the trial's `actual_models`.

## Rubrics and judges

All semantic checks use Jev first and an LLM fallback, including existing state,
call, answer, file and command declarations. There is no deterministic semantic
grader. The task and world-owned evidence establish the intended requirement;
legacy regexes and keywords do not independently decide correctness.

Jev probabilities strictly above 0.9 or strictly below 0.1 settle a check. Uncertain or failed
calls go to the configured LLM panel. A command check is the exception: Jev cannot settle it against the
verifier's literal result (its exit code and stdout matchers), so that disagreement goes to
the panel too. The literal result only routes the question; the judges still decide it. Every fallback judge must agree; null or
disagreement leaves the check ungraded. Missing judges/evidence are INVALID.
These confidence thresholds route model judgements; they are not content rules.

Execution validity is separate. Incomplete execution never automatically means
agent failure. Ambiguous attribution goes to Jev/LLM. Same-family fallback use
is recorded as a caveat, not silently skipped. The new grading policy and Jev
configuration enter resume fingerprints; old heuristic scores are not comparable.

No new quality calibration has been run for this policy. Offline fixtures prove
transport and verdict propagation only. See [judging](jev-judge.md) and
[calibration](../calibration/README.md).

## Contamination

- **Canary.** Every task carries a uuid4 canary, placed in a comment in its
  first instruction and stripped before the agent sees the text. If a model can
  reproduce a canary, it has trained on the corpus.
- **HOLDOUT.** A private repository holds about 30 tasks that mirror the Core
  area mix (`split = "holdout"`). The loader rejects that split inside the
  public tree. HOLDOUT results detect overfitting to the public tasks, and they
  are never shown in logs.
- **TUNE.** Every public task is `split = "tune"` and may be used to improve
  agents.
- **Synthetic data.** People, companies and domains are invented, and domains
  are reserved (`.test`, `.example`). Third-party fixtures carry a `SOURCE.md`.

## Maturity lifecycle

| Maturity | Meaning | In headline results |
|---|---|---|
| draft | A proposal under `review/drafts/`. The loader never discovers it | no |
| candidate | Passes controls. Runs, but has not been piloted | not yet |
| regression | Piloted and discriminates between harnesses | yes |
| aspirational | Piloted, but the best harness scores pass@1 < 0.5 | yes, shown separately |
| retired | Kept for history. Scheduled only when named explicitly | no |

Item analysis looks across harness rows, excluding the controls, for tasks
that no longer discriminate:

- **saturated**: every row scores at least 0.95;
- **floor**: every row scores at most 0.05;
- **noisy**: flake is above 0.25;
- **negative_discrimination**: the point-biserial correlation with total score
  is below 0.

A human moves a flagged task by editing `task.toml` and bumping the task
`version`.

## Reproducibility

- **Resume key.** Each trial's resume key fingerprints these inputs:
  - the task hash;
  - the harness name, version and config hash;
  - the model;
  - the repeat;
  - the judge panel;
  - the Parallax version;
  - the isolation mode and the harness's declared capabilities.
- **Resume.** `--resume` skips keys that already have a non-INVALID verdict.
  After a fingerprint mismatch, the old results are moved aside, not mixed in.
- **Transient invalids are re-run automatically.** A trial that ends INVALID for its own execution
  fault gets up to `--retry-invalid` further attempts inside the run, using the same key as a resume.
  Only INVALID rows are re-run: a PASS or FAIL is never overwritten, and a reason that a re-run cannot
  change (a harness or box the run cannot open, missing evidence, a validity rejection, a failed
  teardown) is never re-run. `results.jsonl` keeps every attempt; readers score one row per key, the
  latest winning.
- **Run manifest.** `manifest.json` embeds the harness infos, the grading
  policy and every task's hash. It travels in each task's evidence zip in
  Langfuse.
- **Redaction.** Everything written to disk passes through a redactor for host
  secrets and token patterns.

## ABC checklist self-assessment

This section assesses Parallax against the Agentic Benchmark Checklist (Zhu et
al., 2025, *Establishing Best Practices for Building Rigorous Agentic
Benchmarks*). The items are grouped under the checklist's three headings. The
status is as of `2.0.0.dev23`, before the first pilot:

- ✅ met by construction and enforced in CI;
- ◐ designed but not yet exercised or complete;
- ✗ not met.

### Task validity

| Item | Status | How |
|---|---|---|
| Tool and environment versions are pinned | ◐ | `uv.lock`, a sidecar image holding the host's dependency versions, the box images keyed by install line, and the harness version recorded per row. The Docker images are not yet built or published |
| External tools are deterministic, with no live APIs or rate limits | ✅ | Every service is a seeded in-process mock. Rate limits exist only as authored faults |
| The environment resets between trials | ✅ | Each trial gets a fresh world from the seed, a fresh token, fresh `workspace` and `home`, and a fresh box |
| The agent cannot reach ground truth or grader code | ◐ | Checks, oracle and mutants are never mounted into the box, and the world exposes only tool calls. Docker isolation is not yet exercised end to end |
| Each task is solvable | ✅ | The oracle must score 1 in `parallax check`, which CI runs on every task |
| Trivial agents cannot pass | ✅ | `null-claim` and `null-refuse` must score 0 on every task |
| Shortcuts and near-misses are caught | ✅ | Mutants must each fail a named check, and every write task needs a collateral mutant |
| Instructions are unambiguous, or the ambiguity is intended | ◐ | Intended ambiguity is scripted (turns, `user_rules`) and paired by the negative-twin rule. Instruction clarity is reviewed by humans, not enforced |

### Outcome validity

| Item | Status | How |
|---|---|---|
| Grading uses state, not the agent's self-report | ✅ | World-owned evidence only. Traces are never graded |
| Answer matching is robust to equivalent forms | ✅ | Normalisation, number and date parsing, and `any_of` aliases. Matchers are unit-tested at their edge cases |
| Substring checks cannot be passed by listing everything | ◐ | `must_not`, `in_order`, `known_urls_only` and mutants that list distractors. Authors must use these. Lint does not force them |
| Side effects and collateral damage are checked | ✅ | `table: "*"` unchanged checks, plus required collateral mutants |
| LLM judges are validated | ◐ | Jev/LLM semantic grading requires fresh human-labelled calibration; offline CI does not establish quality |
| Code outcomes are verified by tests the agent cannot edit | ✅ | Verify runs in a fresh network-less container over the snapshot. `file ... unchanged` guards the test files |
| Evaluation failures are separated from agent failures | ✅ | INVALID has explicit causes and is never counted as FAIL |

### Reporting

| Item | Status | How |
|---|---|---|
| Benchmark and harness code are open | ✅ | MIT code, CC BY 4.0 tasks |
| Contamination is prevented and detectable | ◐ | Canaries and a HOLDOUT split exist. The HOLDOUT repository is not yet populated |
| The construct and its limits are stated | ✅ | This page and the README |
| Uncertainty is reported | ✅ | Clustered-bootstrap CIs, pass^k and paired deltas with ties |
| Statistical power is reported | ◐ | Confidence intervals on every score and delta. No minimum detectable effect is computed |
| Trivial and reference baselines are reported | ✅ | The oracle and null controls, in `parallax check` and as rows of their own |
| Cost is reported with its provenance | ✅ | Metered, harness-reported or unknown, never $0 |
| Known flaws and their impact are reported | ◐ | xfail and item analysis. There are no pilot results yet to quantify their impact |
| There is a plan for maintenance and retirement | ✅ | The maturity lifecycle, task versions in the result key, and retirement by human edit |

### Known limitations

- Scripted users cannot cover open-ended clarification dialogue.
- The mock services model their vendors only as far as their tables and tools.
  They are not API-faithful replicas.
- Many harness manifests are unverified against real binaries (see
  [adapters.md](adapters.md#harness-matrix)).
- Rows from `remote` isolation cannot rule out live web access.
