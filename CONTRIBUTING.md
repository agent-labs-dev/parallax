# Contributing to Parallax

Thanks for helping. Parallax is a benchmark, so its credibility depends on
Jev grading with LLM fallback, honest controls and clean provenance. Most of the
rules below exist to protect one of those three.

New here? [Quick start](docs/quickstart.md) walks through the workflow, and
[Troubleshooting](docs/troubleshooting.md) covers common first failures.

## Setup

```sh
uv sync --locked
uvx pre-commit install    # blocks run output, private names and secrets at commit time
```

Tests run offline; tests that need Docker are marked `@pytest.mark.docker` and
skip themselves when no daemon answers. `uv run pytest tests/smoke` runs the
controls end to end with no keys. `uv run pytest -m langfuse tests/smoke` starts
a throwaway self-hosted Langfuse in Docker, records a run in it, resumes an
interrupted upload and reads every field back.

## The gate

CI is the merge gate, and it runs offline - no secrets, no API keys. Run the
same steps locally before opening a pull request:

```sh
uv sync --locked
uv run ruff format --check parallax tests/core tests/world tests/harnesses scripts/scan_public.py ci/rules/run.py ci/rules/checks
uv run ruff check parallax tests/core tests/world tests/harnesses scripts/scan_public.py ci/rules/run.py ci/rules/checks
uv run ty check --error-on-warning parallax
uv run pytest -q
uv run parallax check core conformance --lint-only    # task structure, offline; no quality grading
uv run --no-project python ci/rules/run.py
```

CI additionally checks that every relative link in the docs resolves and runs
the public scan (`scripts/scan_public.py`, the same check as the pre-commit
hook), so a change to docs or exported files can pass the commands above and
still fail the gate. Run output never belongs in the repository: results live in
Langfuse. `parallax check` runs the same judges as `parallax run` and makes
paid model calls; it is not part of this offline gate.

## Contributing a task

Read [docs/authoring.md](docs/authoring.md) first. In short:

- A task is a directory under `tasks/`. Its checks read only world-owned
  evidence: final world state, the world call log, the workspace snapshot, a
  trusted verify run, or the captured answers.
- Every task must pass `uv run parallax check <task>`. That means the oracle
  scores 1, both null controls score 0, and every mutant fails at least one
  named check.
- Each task needs a semantic gate. Use Jev with LLM fallback; never implement
  heuristic quality grading. Controls need configured judges; offline tests
  verify protocol behavior only. See [judging](docs/jev-judge.md).
- New tasks start as proposals in `review/drafts/`. A maintainer promotes them
  into `tasks/core/`.

## Data rules (enforced by `scripts/scan_public.py`)

- **No real people, companies or customer content.** Use invented names and only
  reserved domains: `.test`, `.example`, `.invalid`, or `example.com`,
  `example.net` and `example.org`.
- **No secrets, tokens or production identifiers**, even expired ones.
- **Third-party data** needs a licence that permits redistribution and
  modification, a `SOURCE.md` in the task (URL, retrieval date, licence,
  SHA-256), and an entry in `THIRD_PARTY.md`.

## Contributing a harness adapter

Read [docs/adapters.md](docs/adapters.md). Prefer a declarative manifest under
`parallax/harnesses/manifests/` over Python. Run the `conformance` canaries
against a new adapter before comparing its results. The planned Agno and Mastra
adapters are on the [roadmap](docs/roadmap.md).

## Code

Python 3.13, typed, Pydantic models at every boundary, `StrEnum` for closed
sets, and exhaustive `match` statements. Comments explain why, not what. Keep
pull requests focused and use Conventional Commit titles (`feat:`, `fix:`,
`docs:`, ...).

## Releases

Maintainers release by setting `__version__` in `parallax/__init__.py`, merging,
then pushing a matching tag such as `v2.0.0`. The release workflow builds the
package, checks the wheel runs on its own and publishes it to PyPI. Workflows
written by `parallax ci init` pin the version that wrote them.

## Licensing of contributions

By submitting a contribution you agree that it is licensed under MIT (code) or
CC BY 4.0 (anything under `tasks/`), matching `LICENSE` and `LICENSE-DATA`,
and that you have the right to submit it.

## Reporting problems

Security vulnerabilities go through [SECURITY.md](SECURITY.md), never a public
issue. Conduct concerns go to `CODE_OF_CONDUCT.md`.

## Conduct

Participation is governed by `CODE_OF_CONDUCT.md`.
