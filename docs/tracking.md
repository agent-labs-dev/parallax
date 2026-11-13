# Results in Langfuse

Every Parallax run is recorded in a [Langfuse](https://langfuse.com) project,
which is the only store of results. Parallax stays the evaluator: Langfuse holds
what a run measured, and nothing there is re-run or re-judged. The
[quickstart](quickstart.md) covers configuration and the commands; this page
describes what is recorded and how runs are compared.

## What is recorded

| Parallax | Langfuse |
|---|---|
| a task at a version | a dataset item `<dataset>:<task>@v<version>` in the dataset named by `--dataset` (default `parallax`) |
| one harness/model row of a run | an experiment named `<start> <harness>/<model> <run id>` |
| one task in that row (all repeats) | an experiment item, whose trace is named after the task and versioned by the row |
| per-task results | scores: `outcome` (pass, fail, flaky, invalid), `pass_rate`, `partial_credit`, `cost_usd`, `agent_seconds` |
| per-row results | experiment scores: `pass_at_1`, `pass_all_repeats` (pass^k), `partial_credit`, `cost_per_task_usd`, `agent_p50_seconds`, `valid_trials`, `invalid_trials` |

Each experiment item's output holds the task's verdicts, the checks that
failed, cost, timing, harness version and config hash, the models that served
the trials, and a record of every repeat. It also carries an **evidence zip**:
the task's trial directories, every result row including retried attempts, and
the run's own files. Trace tags (`run:`, `harness:`, `model:`, `area:`) filter
Langfuse's trace and score views to one run, harness, model or area.

Experiment metadata records what the run measured: the run id, harness
versions and config hashes, judge models and a judge policy hash, the Parallax
version and revision, repeats, isolation, and every `--meta KEY=VALUE` given to
`parallax run`.

A cost that was not observed stays unknown and is never recorded as 0. Costs are
the provider's measured usage, not invoice verification.

Experiments are recorded through OpenTelemetry span attributes, as the Langfuse
SDK's experiment runner records them, which needs Langfuse Cloud or self-hosted
Langfuse v4.59.0 or later.

## Uploads

A run uploads itself when it finishes. Parallax seals the run directory (every
file with its hash), pushes it, then reads every field back and downloads each
evidence zip to check its bytes. The local directory is deleted only when all of
it matches.

Every write lands on a fixed id: an experiment's id derives from the project,
dataset and experiment name, and each item's trace from the experiment and the
item. So an upload that stopped part way is finished by
`parallax upload --resume`, which writes what is missing and never duplicates
anything. Scores carry the run's start time, so charts place a run when it ran
even if it was uploaded later.

## Versioning

A task's `version` (bumped whenever its grading changes) is part of its item id,
so only trials graded the same way are ever paired. Older experiments keep
pointing at the version they ran.

Runs pair only when they measured the same thing. A row is reported as not
compared, with the reason, when the two runs differ in:

- the judge configuration that graded them (judge policy version, Jev model and
  endpoint, fallback endpoint and judge models), or a run predates that record;
- the version of a shared task;
- harness versions or configs;
- the Parallax revision.

`--allow-identity-change` compares such runs anyway. A run with more than 10%
INVALID trials is never compared, because it measures the environment rather
than the agent.

## Regressions

`parallax compare` takes the paired per-task pass@1 difference over the tasks
both runs share, with a task-clustered bootstrap 95% confidence interval. A row
is **regressed** when the whole interval is below zero, **improved** when it is
above, and **tied** otherwise. Tasks that flipped from passing to failing are
listed for every row, because a flip in a small run deserves a look even when
the row is tied. Under `--fail-on-regression`, a row that could not be compared
fails the gate too: it has not been shown to be free of regressions. For the
power a run gives, see [methodology](methodology.md#statistics).

Each compared row links to Langfuse's side-by-side view of the two experiments,
which shows every score and the recorded repeats item by item.
