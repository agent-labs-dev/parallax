# Parallax

Parallax evaluates agent harnesses against simulated worlds. Setup, the pull request process and the
review rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Checks

The offline gate, the same steps CI runs. It needs no keys and spends nothing:

```sh
uv sync --locked
uv run ruff format --check parallax tests/core tests/world tests/harnesses scripts/scan_public.py ci/rules/run.py ci/rules/checks
uv run ruff check parallax tests/core tests/world tests/harnesses scripts/scan_public.py ci/rules/run.py ci/rules/checks
uv run ty check --error-on-warning parallax
uv run pytest -q
uv run parallax check core conformance --lint-only
uv run --no-project python ci/rules/run.py
```

## Rules

- Eval campaigns and paid smoke runs go through the `Parallax evals` workflow, never a local
  `parallax run`: dispatch it with `gh workflow run parallax.yml -f suite=... -f harness=... -f model=...`
  (inputs `repeats`, `concurrency`, `note` and `runner` too). Each dispatch gets a fresh runner, so every
  run starts from the same clean machine. Offline tests and checks still run locally.
- The workflow file is generated: change the template in `parallax/ci.py`, then regenerate it with
  `parallax ci init --suite conformance --harness oracle --repeats 1 --dispatch-runner
  blacksmith-16vcpu-ubuntu-2404 --from-checkout --force`.
- The repository's own rules live in `ci/rules/checks/`, one file per rule id with fixtures under
  `ci/rules/check-tests/<id>/`. Suppress a hit with a `# rule-ignore: <id> - <reason>` comment line at
  most three lines above it, the reason stating why the site is safe. `run.py --test` runs the fixtures.

## Standards

- `repo-standards`: the RS-* requirements every agent-labs-dev repo meets (repo, Actions and security settings,
  rulesets, CI hardening, community files); its `audit.py` checks this repo.
