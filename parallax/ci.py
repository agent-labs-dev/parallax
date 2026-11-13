"""`parallax ci init`: a GitHub workflow that evaluates every pull request against the default branch.

A push to the default branch records the baseline. A pull request records its own run, compares
each row with the newest default-branch run of that row and keeps one comment on the pull request
up to date with the difference. A manual dispatch runs a campaign on a clean runner, with its own
selection, harness, model, repeats and concurrency; it never becomes a branch's baseline. The job
is inert until the repository names its Langfuse project.

Every run spends inside a campaign budget (`parallax run --budget --reserve`). A dispatch names its
own; a push or pull request runs only once the repository sets one, so opening a pull request in a
repository without a budget spends nothing.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from parallax import __version__, jev
from parallax.judge import DEFAULT_MODEL

MARKER = "<!-- parallax-evals -->"
JUDGE_MODEL = DEFAULT_MODEL

TEMPLATE = """\
# Written by `parallax ci init`. Settings: repository variables LANGFUSE_BASE_URL,
# PARALLAX_LANGFUSE_PROJECT and optionally PARALLAX_JUDGE_MODELS (the fallback panel); secrets
# LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and OPENROUTER_API_KEY (Jev and the fallback panel), and
# PARALLAX_UPSTREAM_OPENAI_KEY or PARALLAX_UPSTREAM_ANTHROPIC_KEY for the wire the models use.
# Each run's report goes to the step summary, a pull request's comment and, for {branch}, the
# "Parallax eval results" issue. PARALLAX_REPORT_REASONS=true (a variable) quotes invalid trials'
# reasons, which can quote the agent's work. Secrets PARALLAX_REPORT_WEBHOOK_URL and
# PARALLAX_REPORT_WEBHOOK_SECRET also send {branch} and dispatched runs' reports to a webhook,
# signed as hex HMAC-SHA256 of the body in X-Webhook-Signature.
# Every run spends inside a campaign budget, in USD: a dispatch takes its budget and per-trial
# reserve as inputs, and push and pull request runs use the variables PARALLAX_BUDGET_USD and
# PARALLAX_RESERVE_USD; until both are set, those runs are skipped.
# Until PARALLAX_LANGFUSE_PROJECT is set, the job is skipped. Pull requests from forks
# are skipped too: they get no secrets and cannot comment.
name: Parallax evals

on:
  pull_request:
  push:
    branches: [{branch}]
  # A campaign: each dispatch gets a fresh runner, so every run starts from the same clean machine.
  # A blank input takes the default this workflow was written with.
  workflow_dispatch:
    inputs:
      suite:
        description: Suites, task ids, globs or filters, separated by spaces
        default: {suite_input}
      harness:
        description: Harness
        default: {harness_input}
      model:
        description: Model, blank for the default
        default: {model_input}
      repeats:
        description: Repeats per task
        default: "{repeats}"
      concurrency:
        description: Trials at once
        default: "4"
      note:
        description: Experiment description, e.g. what changed
        default: ""
      budget:
        description: Campaign spend limit in USD
        required: true
      reserve:
        description: What each trial reserves of the budget before it starts, in USD
        required: true
      runner:
        description: Runner label
        default: {dispatch_runner}

# Each job widens these for itself.
permissions: {{}}

concurrency:
  # A newer push supersedes a pull request's run; each {branch} commit keeps its own run. A dispatched
  # campaign is a group of its own: a push never cancels it, and it never cancels another.
  group: ${{{{ github.workflow }}}}-${{{{ github.event_name == 'workflow_dispatch' && github.run_id || github.event.pull_request.number || github.ref }}}}
  cancel-in-progress: ${{{{ github.event_name == 'pull_request' }}}}

jobs:
  evaluate:
    name: evaluate
    if: >-
      vars.PARALLAX_LANGFUSE_PROJECT != '' &&
      (github.event_name != 'pull_request' || !github.event.pull_request.head.repo.fork) &&
      (github.event_name == 'workflow_dispatch' || (vars.PARALLAX_BUDGET_USD != '' && vars.PARALLAX_RESERVE_USD != ''))
    runs-on: ${{{{ inputs.runner || {runner} }}}}
    timeout-minutes: ${{{{ github.event_name == 'workflow_dispatch' && 360 || 90 }}}}
    permissions:
      contents: read
      issues: write  # the results issue for {branch} runs
      pull-requests: write  # the comment on a pull request
    env:
      LANGFUSE_BASE_URL: ${{{{ vars.LANGFUSE_BASE_URL }}}}
      LANGFUSE_PUBLIC_KEY: ${{{{ secrets.LANGFUSE_PUBLIC_KEY }}}}
      LANGFUSE_SECRET_KEY: ${{{{ secrets.LANGFUSE_SECRET_KEY }}}}
      PARALLAX_LANGFUSE_PROJECT: ${{{{ vars.PARALLAX_LANGFUSE_PROJECT }}}}
      OPENROUTER_API_KEY: ${{{{ secrets.OPENROUTER_API_KEY }}}}
      # Without a fallback panel, a question Jev leaves uncertain makes the trial INVALID.
      PARALLAX_JUDGE_BASE_URL: {judge_base_url}
      PARALLAX_JUDGE_API_KEY: ${{{{ secrets.OPENROUTER_API_KEY }}}}
      PARALLAX_JUDGE_MODELS: ${{{{ vars.PARALLAX_JUDGE_MODELS || '{judge}' }}}}
      # The metering proxy's provider keys; a wire without one answers 503.
      PARALLAX_UPSTREAM_OPENAI_KEY: ${{{{ secrets.PARALLAX_UPSTREAM_OPENAI_KEY }}}}
      PARALLAX_UPSTREAM_ANTHROPIC_KEY: ${{{{ secrets.PARALLAX_UPSTREAM_ANTHROPIC_KEY }}}}
      BRANCH: ${{{{ github.head_ref || github.ref_name }}}}
      RUN_ID: gh-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}
      PARALLAX: {parallax}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2 # v10.3.3
{install}
      - name: Evaluate
        env:
          SUITE: ${{{{ inputs.suite }}}}
          HARNESS: ${{{{ inputs.harness }}}}
          MODEL: ${{{{ inputs.model }}}}
          REPEATS: ${{{{ inputs.repeats }}}}
          CONCURRENCY: ${{{{ inputs.concurrency }}}}
          NOTE: ${{{{ inputs.note }}}}
          BUDGET: ${{{{ inputs.budget || vars.PARALLAX_BUDGET_USD }}}}
          RESERVE: ${{{{ inputs.reserve || vars.PARALLAX_RESERVE_USD }}}}
        run: |
          set -f  # task globs are for Parallax to expand, not the shell
          select=${{SUITE:-{select}}}
          harness=${{HARNESS:-{harness}}}
          model=${{MODEL:-{model}}}
          meta=(--meta "commit=$GITHUB_SHA")
          # A dispatched campaign is no branch's baseline.
          if [ "${{GITHUB_EVENT_NAME:-}}" != workflow_dispatch ]; then meta+=(--meta "branch=$BRANCH"); fi
          # shellcheck disable=SC2086 # the selection is several words
          $PARALLAX run $select --harness "$harness" ${{model:+--model "$model"}} \\
            --repeats "${{REPEATS:-{repeats}}}" --concurrency "${{CONCURRENCY:-4}}" --isolation {isolation} \\
            --budget "${{BUDGET:?every run needs a campaign budget}}" \\
            --reserve "${{RESERVE:?every run needs a per-trial reserve}}" \\
            --out "runs/$RUN_ID" --note "${{NOTE:-$BRANCH ${{GITHUB_SHA::8}}}}" "${{meta[@]}}"
      - name: Finish an interrupted upload
        if: failure()
        run: $PARALLAX upload --resume
      - name: Keep runs Langfuse did not take
        if: failure()
        uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        with:
          name: parallax-runs-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}
          path: runs/
          include-hidden-files: true
          if-no-files-found: ignore
      - name: Report
        id: report
        # A failed run still reports whatever reached Langfuse; a report that fails leaves the job as it was.
        if: ${{{{ !cancelled() }}}}
        continue-on-error: true
        env:
          REASONS: ${{{{ vars.PARALLAX_REPORT_REASONS == 'true' && '--reasons' || '' }}}}
          RUN_URL: ${{{{ github.server_url }}}}/${{{{ github.repository }}}}/actions/runs/${{{{ github.run_id }}}}
        run: |
          # shellcheck disable=SC2086 # REASONS is one flag or nothing
          $PARALLAX report "$RUN_ID" $REASONS --link "Workflow run=$RUN_URL" > report.md
          cat report.md >> "$GITHUB_STEP_SUMMARY"
      - name: Compare with {branch}
        if: github.event_name == 'pull_request' && !cancelled()
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          PR: ${{{{ github.event.pull_request.number }}}}
        run: |
          {{
            echo "{marker}"
            echo "### Parallax: $RUN_ID on $BRANCH"
            echo
            cat report.md 2>/dev/null || echo "No report: the run did not finish."
            echo
            echo "#### Against the newest {branch} run"
            echo
            $PARALLAX compare "$RUN_ID" --baseline "branch={branch}"{compare_flags} || true
          }} > delta.md
          # Edit this workflow's own comment, found by its marker, never another bot's.
          mine='.[] | select(.body | startswith("{marker}")) | .id'
          id=$(gh api "repos/$GITHUB_REPOSITORY/issues/$PR/comments" --paginate --jq "$mine" | tail -n 1)
          if [ -n "$id" ]; then
            gh api -X PATCH "repos/$GITHUB_REPOSITORY/issues/comments/$id" -F body=@delta.md > /dev/null
          else
            gh pr comment "$PR" --body-file delta.md
          fi
      - name: Post to the results issue
        if: github.event_name == 'push' && steps.report.outcome == 'success'
        env:
          GH_TOKEN: ${{{{ github.token }}}}
        run: |
          title="Parallax eval results"
          issue=$(gh issue list --state open --search "in:title \\"$title\\"" --json number,title \\
            --jq "map(select(.title == \\"$title\\")) | .[0].number // empty")
          if [ -z "$issue" ]; then
            issue=$(gh issue create --title "$title" \\
              --body "Every {branch} run's Parallax report, newest last." | grep -o '[0-9]*$')
          fi
          gh issue comment "$issue" --body-file report.md
      - name: Send the report to a webhook
        if: github.event_name != 'pull_request' && steps.report.outcome == 'success'
        env:
          WEBHOOK_URL: ${{{{ secrets.PARALLAX_REPORT_WEBHOOK_URL }}}}
          WEBHOOK_SECRET: ${{{{ secrets.PARALLAX_REPORT_WEBHOOK_SECRET }}}}
          RUN_URL: ${{{{ github.server_url }}}}/${{{{ github.repository }}}}/actions/runs/${{{{ github.run_id }}}}
        run: |
          [ -n "$WEBHOOK_URL" ] && [ -n "$WEBHOOK_SECRET" ] || exit 0
          jq -n --rawfile report report.md \\
            '{{source: "parallax", repository: env.GITHUB_REPOSITORY, event: env.GITHUB_EVENT_NAME,
              run_id: env.RUN_ID, branch: env.BRANCH, run_url: env.RUN_URL, report_markdown: $report}}' > payload.json
          signature=$(openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex < payload.json | sed 's/^.* //')
          curl -fsS -X POST "$WEBHOOK_URL" -H "Content-Type: application/json" \\
            -H "X-Webhook-Signature: $signature" --data-binary @payload.json > /dev/null \\
            || echo "::warning::the report webhook refused the report"
"""


def render(
    *,
    suite: str,
    harness: str,
    model: str | None,
    repeats: int,
    install: str,
    isolation: str,
    branch: str,
    runner: str = "ubuntu-24.04",
    dispatch_runner: str | None = None,
    from_checkout: bool = False,
) -> str:
    # A label lands in the YAML and in a quoted expression, so it must need no escaping in either.
    if bad := [r for r in (runner, dispatch_runner) if r is not None and not re.fullmatch(r"[A-Za-z0-9._-]+", r)]:
        raise ValueError(f"runner label {bad[0]!r}: use letters, digits, '.', '_' and '-'")
    # A block scalar, so any shell (several lines, `: `, ` #`) reaches the runner unchanged.
    body = "".join(f"          {line}\n" if line.strip() else "\n" for line in install.strip().splitlines())
    step = f"      - name: Install the harness\n        run: |\n{body}" if install.strip() else ""
    # The selection reaches the shell as one word that splits on spaces, like the dispatch input.
    select = " ".join(shlex.split(suite))
    return TEMPLATE.format(
        branch=branch,
        marker=MARKER,
        # From a checkout, the Parallax revision is what the pull request changes, so a differing one is paired.
        compare_flags=" --allow-identity-change" if from_checkout else "",
        parallax="uv run --locked parallax" if from_checkout else f"uvx --from parallax-evals=={__version__} parallax",
        judge_base_url=jev.DEFAULT_BASE_URL,
        judge=JUDGE_MODEL,
        install=step.rstrip("\n"),
        select=shlex.quote(select),
        harness=shlex.quote(harness),
        model=shlex.quote(model) if model else "",
        repeats=repeats,
        isolation=isolation,
        # JSON strings are YAML strings, and a quoted one is never read as a number or a bool.
        suite_input=json.dumps(select),
        harness_input=json.dumps(harness),
        model_input=json.dumps(model or ""),
        runner=f"'{runner}'",
        dispatch_runner=json.dumps(dispatch_runner or runner),
    )


def write_workflow(
    path: Path,
    *,
    suite: str,
    harness: str,
    model: str | None,
    repeats: int,
    install: str,
    isolation: str,
    branch: str,
    force: bool,
    runner: str = "ubuntu-24.04",
    dispatch_runner: str | None = None,
    from_checkout: bool = False,
) -> Path:
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; --force replaces it")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render(
        suite=suite, harness=harness, model=model, repeats=repeats, install=install, isolation=isolation, branch=branch,
        runner=runner, dispatch_runner=dispatch_runner, from_checkout=from_checkout,
    )  # fmt: skip
    path.write_text(text, encoding="utf-8")
    return path
