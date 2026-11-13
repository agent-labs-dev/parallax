<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/logo-lockup-dark.svg">
  <img src="assets/brand/logo-lockup.svg" alt="Parallax" width="480">
</picture>

**A reproducible benchmark for agent harnesses, graded on what they did to the world.**

[![ci](https://img.shields.io/github/actions/workflow/status/agent-labs-dev/parallax/ci.yml?branch=main&style=flat-square&label=ci)](https://github.com/agent-labs-dev/parallax/actions/workflows/ci.yml)
[![pypi](https://img.shields.io/pypi/v/parallax-evals?include_prereleases&style=flat-square)](https://pypi.org/project/parallax-evals/)

</div>

Most agent benchmarks score a model inside one fixed scaffold. In production,
though, the model runs inside a product. That product is the **harness**, and it
decides the prompts, tools, memory, retries, approvals and model routing. Two
harnesses on the same model can differ more than two models in the same
harness. Parallax measures the harness and model together, end to end, and
records every run in [Langfuse](https://langfuse.com):

- **Same tasks, any harness.** Every harness gets the same seeded services (mail,
  chat, calendar, drive, sheets, payments, a scheduler, a seeded web, and more)
  over one MCP server. A task is a directory of data, not code written for one
  harness.
- **Graded on world state, not self-report.** Checks read four things: the
  final service state, the server's own call log, the workspace snapshot and a
  trusted test run. What the harness says it did is kept for diagnosis only.
- **Every task is proven gradeable.** A scripted oracle must pass every task.
  Two null agents must fail it: one claims success and does nothing, the other
  asks a question and does nothing. Seeded near-miss mutants must be caught.
- **Scores, reliability and cost.** pass@1 comes with task-clustered confidence
  intervals, pass^k measures reliability, and cost comes from a metering proxy
  when available. Unknown cost stays unknown, never `$0`.
- **Regressions, not just scores.** `parallax compare` pairs a run with an
  earlier one task by task and reports the pass@1 delta with a 95% confidence
  interval, so a pull request can be gated on it.

## Quick start

You need Docker, Python 3.13, [uv](https://docs.astral.sh/uv/) and a Langfuse
project (Langfuse Cloud, or self-hosted v4.59.0 or later). Install the CLI; its
releases are pre-releases, so uv needs `--prerelease allow`:

```sh
uv tool install --prerelease allow parallax-evals
```

Check the install offline, with no keys: this lints the five `conformance`
tasks and should print `5 tasks, 0 problems`.

```sh
parallax check conformance --lint-only
```

Point it at Langfuse, the judge and a model provider:

```sh
export LANGFUSE_BASE_URL=https://cloud.langfuse.com
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export PARALLAX_LANGFUSE_PROJECT=my-evals    # the project id or name results belong in
export OPENROUTER_API_KEY=...                # the Jev judge
export PARALLAX_UPSTREAM_OPENAI_KEY=...      # the meter's OpenAI-wire key (OpenRouter by default)
```

Run a suite against a harness, then compare it with the previous run:

```sh
parallax run lite --harness hermes --model openai/gpt-6-sol
parallax compare
```

A run uploads itself to Langfuse when it finishes and prints a link to each
experiment. The [quickstart](docs/quickstart.md) walks through the whole
workflow, including the task controls, `show`, and gating pull requests
with `parallax ci init`. [Troubleshooting](docs/troubleshooting.md) covers
common setup failures.

## Agent harnesses

Parallax focuses on general-purpose agents: connected services, multi-step
work, persistent memory and delegation. The in-tree harnesses are
`hermes` and `openclaw`, plus the
`oracle`, `null-claim` and `null-refuse` controls. The
[harness matrix](docs/adapters.md#harness-matrix) records what has been verified
for each. Docker is the default isolation boundary.

Desktop tasks declare `desktop` and run seeded GTK apps in a private X11 device
box. A harness that declares `desktop` controls them through its own computer-use
tools; grading reads the apps' saved workspace files. Other harnesses are NA. See the
[desktop recipe](docs/authoring.md#desktop-apps) for image building, offline GUI
smoke tests, and task authoring.

## Suites

| Suite | Tasks | Use |
|---|---|---|
| `core` | Tasks in 10 areas: saas, precision, recovery, safety, code, files, research, restraint, memory, schedule | The benchmark |
| `lite` | 20-task subset of core, weighted to work in connected apps | Smoke and iteration. It is not ranking-validated until Kendall τ ≥ 0.8 against core |
| `hard` | 20-task subset of core: long, vague work across 3 or more connected apps, with more data and traps | Longer workflows after lite |
| `flagship` | 10-task subset of core, one per kind of everyday assistant work | The headline cross-harness comparison |
| `conformance` | 5 capability canaries | Checking a new adapter before it is compared |

Each suite's `suite.toml` names its owner and purpose; a subset suite such as `lite` lists its task ids
there, with `within` naming the suite they come from. `parallax check --lint-only` checks every listed id.

A selection can mix suites, task ids, globs, paths and filters, for example
`parallax run core area=safety requires<=mcp,multi_turn ...`. Plugin packages
add suites through the `parallax.suites` entry point, and private task roots are
added with `--tasks DIR`; a draft under such a root, such as `review/drafts`, is selectable by id.
A plugin suite's first root holds its `suite.toml`.

## Adding a harness

Most harnesses need no Python. A TOML manifest gives the launch argv, how the
world MCP server is mounted, how the harness reaches the metering proxy, and
which of the closed set of output parsers applies. Anything else, such as an
SDK, a remote API or a bridge, implements a four-method protocol and registers
through the `parallax.harnesses` entry point. See
[docs/adapters.md](docs/adapters.md) for the protocol, the manifests and the
in-tree harnesses.

## Adding a task

A task is a Harbor-compatible directory. It holds `task.toml`, one instruction
per turn, a seed for the world, `checks.yaml`, `mutations.yaml`, and an oracle.
It ships only when `parallax check` passes. See
[docs/authoring.md](docs/authoring.md) for the format, the check kinds and the
lint rules, and [docs/taxonomy.md](docs/taxonomy.md) for the failure modes.
New tasks start as proposals in [review/drafts/](review/drafts/README.md).

## Methodology

[docs/methodology.md](docs/methodology.md) covers these topics:

- verdicts, and the precedence NA > INVALID > FAIL > PASS;
- the statistics;
- cost provenance;
- web isolation;
- judge calibration;
- contamination controls (canaries and a private holdout);
- the task maturity lifecycle;
- a self-assessment against the Agentic Benchmark Checklist.

[docs/tracking.md](docs/tracking.md) describes what a run records in Langfuse
and how comparisons pair runs.

## Status

`2.0.0.dev21`. The task format, world, checks, controls, statistics and Langfuse
recording are in place. No pilot has been run, so no cross-harness results are
published yet.

## Contributing and licence

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

Code is licensed under [MIT](LICENSE). Task data under `tasks/` is
licensed under [CC BY 4.0](LICENSE-DATA). Third-party fixtures keep their own
licences, which are listed in [THIRD_PARTY.md](THIRD_PARTY.md).
