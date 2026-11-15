# Quick start

This page takes you from installing Parallax to a pull request that is gated on
its results. Every run is recorded in Langfuse; nothing is kept locally once
Langfuse holds it. For the ideas behind the numbers, read
[methodology](methodology.md).

- [Requirements](#requirements)
- [Install](#install)
- [Configure](#configure)
- [Check the tasks](#check-the-tasks)
- [Run](#run)
- [Read the results](#read-the-results)
- [Compare runs](#compare-runs)
- [Gate pull requests](#gate-pull-requests)
- [Exit codes](#exit-codes)
- [Before you compare](#before-you-compare)

## Requirements

- Python 3.13 or newer, and [uv](https://docs.astral.sh/uv/).
- Docker, the default isolation boundary for harnesses.
- A Langfuse project: Langfuse Cloud, or self-hosted Langfuse v4.59.0 or later.
- A judge key, and a provider key for the models under test. Judging and model
  calls cost money.

## Install

```sh
uv tool install --prerelease allow parallax-evals
parallax --version
```

To work on Parallax itself, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Configure

Parallax reads its configuration from the environment.

| Variable | Purpose |
|---|---|
| `LANGFUSE_BASE_URL` (or `LANGFUSE_HOST`) | The Langfuse server |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | API keys for the project results go in |
| `PARALLAX_LANGFUSE_PROJECT` | The id or name of that project. Writes are refused when the keys belong to any other project, so eval data never lands in a production tracing project |
| `OPENROUTER_API_KEY` | The Jev judge, through OpenRouter |
| `PARALLAX_JUDGE_MODELS`, `PARALLAX_JUDGE_BASE_URL`, `PARALLAX_JUDGE_API_KEY` | Optional: the LLM judge fallback. Unset, it is DeepSeek V4.1 Flash 1017 on OpenRouter with `OPENROUTER_API_KEY` |
| `PARALLAX_UPSTREAM_OPENAI_KEY`, `PARALLAX_UPSTREAM_ANTHROPIC_KEY` | The metering proxy's provider keys, one per wire your models use |

The OpenAI wire defaults to OpenRouter; see
[running real harnesses](adapters.md#running-real-harnesses) to call a provider
directly. Provider keys never enter the harness's box: the meter swaps them in.
[Judging](jev-judge.md) covers the judge settings.

## Check the tasks

```sh
parallax check lite --lint-only    # task structure, offline and free
parallax check lite                # oracle passes, null controls fail, mutants caught (judged, paid)
```

`check` proves each task is gradeable before you measure anything with it. Run
it after changing a task or a judge setting.

## Run

```sh
parallax run lite --harness hermes --model openai/gpt-6-sol
```

`--harness` and `--model` take comma-separated lists; every harness runs with
every model, `--repeats` times (default 3). `--dry-run` lists the selected tasks
and runs nothing. A turn waits for the harness to finish, however long it takes;
`--turn-timeout SECONDS` kills a turn still running after that and records the
trial as a timeout, so a hung harness cannot hold up a paid run. Parallax never
caps spend itself, so size a paid run by its trial count. A run goes through
these steps:

1. It confirms Langfuse accepts writes for the configured project, before
   anything is spent.
2. It runs every trial in `runs/<timestamp>` (or `--out DIR`) and judges it.
3. It seals the run directory, uploads it, and reads every field back from
   Langfuse, including each evidence zip's bytes.
4. Only when everything matches does it delete the local directory, and print
   a link to each experiment.

Record context with `--note` (the experiment description) and `--meta KEY=VALUE`
(experiment metadata, which `compare --baseline` matches on):

```sh
parallax run lite --harness hermes --model openai/gpt-6-sol --meta branch=main --note "new system prompt"
```

**Interrupted runs.** `parallax run ... --resume runs/<dir>` finishes the
trials of a run that stopped part way. If the run finished but the upload
failed, the run stays under `runs/` and `parallax upload --resume` completes
it. Every write uses a fixed id, so retrying never duplicates anything.

## Read the results

```sh
parallax show              # recorded runs, newest first, with pass@1 and pass^k
parallax show <run-id>     # one run, task by task
```

In Langfuse, each task version is a dataset item and each harness/model row of
a run is an experiment. [Tracking](tracking.md) lists the scores and what each
item carries.

## Compare runs

```sh
parallax compare                                   # the newest run against the previous run of each row
parallax compare <run-id> --baseline branch=main   # against the newest earlier run with that metadata
parallax compare <run-id> --baseline <run-id>      # against one named run, even the oldest
```

`--baseline` takes a run id or an experiment name as well as `KEY=VALUE`
metadata filters, the same forms the candidate takes. A named run still has
to match any metadata filters passed alongside it.

For each harness/model row, `compare` pairs the tasks both runs share and
reports the pass@1 delta with a task-clustered bootstrap 95% confidence
interval, the cost and time deltas, the tasks that flipped, and a side-by-side
link in Langfuse. A row regressed when its interval lies wholly below zero.
`--fail-on-regression` exits 1 on a regression, or when a row could not be
compared.

## Gate pull requests

```sh
parallax ci init --suite lite --harness openclaw --model openai/gpt-6-sol --install "npm i -g openclaw"
```

This writes `.github/workflows/parallax.yml`. Set the repository variables
`LANGFUSE_BASE_URL` and `PARALLAX_LANGFUSE_PROJECT`, and the secrets
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `OPENROUTER_API_KEY`, plus
`PARALLAX_UPSTREAM_OPENAI_KEY` or `PARALLAX_UPSTREAM_ANTHROPIC_KEY` for each wire
your models use. The job is skipped until `PARALLAX_LANGFUSE_PROJECT` is set.

`--upstream` names those wires, from `openai` and `anthropic` (both by default):
the workflow carries only the provider keys it names. `--report-sink` names
where a branch or dispatched run's report also goes, from `issue` and `webhook`
(both by default), on top of the step summary and a pull request's comment. Pass
an empty value to write neither, such as `--upstream ""` for a run with no
metered wire.

Every run spends inside a campaign budget in USD (`parallax run --budget
--reserve`). Push and pull request runs take theirs from the variables
`PARALLAX_BUDGET_USD` and `PARALLAX_RESERVE_USD`, and are skipped until both
are set, so opening a pull request spends nothing until you choose a limit. A
dispatch names its own `budget` and `reserve`. Then:

- a push to the default branch records the baseline and comments its report on
  the open "Parallax eval results" issue, creating it the first time;
- a pull request records its own run, compares it with the newest default-branch
  run of each row, and keeps one comment on the pull request up to date with the
  report and the comparison;
- a failed upload is finished with `parallax upload --resume` in the same job;
- a manual dispatch runs a campaign on a fresh runner, with its own `suite`,
  `harness`, `model`, `repeats`, `concurrency`, `note`, `budget`, `reserve`
  and `runner` inputs. It
  is not recorded as the branch's baseline.

Every run writes its report to the job summary. The report is what
`parallax report <run>` prints: the tally, cost, and every trial that did not
pass with the checks it failed or why it was invalid, linked to the Langfuse
experiment. Langfuse keeps only a hash of an invalid reason, because the text can
quote the agent's work. Set the variable `PARALLAX_REPORT_REASONS=true` to quote
it from the evidence, in a repository where everyone who reads the report may
read the evidence. Set the secrets `PARALLAX_REPORT_WEBHOOK_URL` and
`PARALLAX_REPORT_WEBHOOK_SECRET` to also send default-branch and dispatched
reports to a webhook as JSON (`report_markdown`, `run_id`, `branch`, `run_url`),
signed as the hex HMAC-SHA256 of the body in `X-Webhook-Signature`. The workflow
carries the webhook step unless `--report-sink` left it out.

`--runner` picks the runner label for push and pull request runs, and
`--dispatch-runner` the default for a dispatch, such as a larger runner for a
high-concurrency campaign.

The workflow pins the Parallax version that wrote it; `--from-checkout` runs the
checked-out commit's Parallax instead, for Parallax's own repository; its pull
request comparison then pairs runs across the Parallax revision the pull request changes. `--force` replaces an
existing workflow.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | A failed check, a run with more than 10% INVALID trials, or a regression under `--fail-on-regression` |
| 2 | A usage error |
| 3 | The environment: Docker, the world failed to boot, or Langfuse |

## Before you compare

- **Versioning.** Runs pair only when they measured the same thing: judge
  policy and models, Parallax revision, task versions, and harness version and
  config. A change in any of these moves verdicts by itself, so `compare`
  reports such a row as not compared. `--allow-identity-change` pairs them
  anyway.
- **Isolation.** `--isolation host` runs the harness on your machine with
  native web tools off by flag; it is for development, not a grading boundary.
  Docker isolation closes the web path, and `remote` cannot rule out live web
  access.
- **Cost.** Cost carries provenance: metered, harness-reported, or unknown.
  Only metered costs are comparable across harnesses.
