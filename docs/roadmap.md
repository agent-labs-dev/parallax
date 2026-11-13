# Roadmap

Parallax is at `2.0.0.dev21`. The task format, the simulated world, the checks,
the controls, the statistics and Langfuse recording are in place and exercised by CI.
This page is directional; it is not a commitment to dates.

## Focus

Parallax targets **general-purpose agent runtimes** - SDK loops, CLI agents
and remote product APIs - not niche verticals or single-vendor
coding agents. A harness is anything that can receive a turn and act on a
mounted world, so the more general-purpose runtimes Parallax can measure the
same way, the more useful a cross-harness comparison is. That is why the adapter
surface is mostly declarative (a TOML manifest, or a
four-method Python protocol) and why a new runtime should need little or no
code.

## Near term: publish pilot results

- **Verify the remaining harness manifests against real binaries.** Several
  in-tree manifests are still marked unverified in the
  [harness matrix](adapters.md#harness-matrix); each needs its flags rechecked
  and real turns recorded.
- **Run and publish a pilot.** The first headline comparison across a
  spread of harnesses and models exercises cost, latency and reliability
  end to end.
- **Exercise Docker isolation end to end.** Box images build on demand from
  source; the first published results should run under the default Docker
  isolation rather than host-only development.

## Harness follow-ups

General-purpose runtimes first, then more SDKs:

- **Agno**, a Python agent framework, and **Mastra**, a
  TypeScript agent framework, are the next planned adapters. Their adapter
  surface (CLI, or a Python/Node SDK bridge) is chosen once each is
  checked against its current headless and SDK interfaces, not assumed ahead
  of time.
- Continue verifying the general-purpose harnesses already in the tree, such
  as **Hermes** and **OpenClaw**.

Every added harness passes the `conformance` canaries before its results are
compared.

## Task corpus

- Grow `core` across its ten areas, keeping the oracle-must-pass and
  both-nulls-must-fail controls green.
- Keep the holdout split populated in its private repository, mirroring the
  core area mix, so the public tasks can detect overfitting. The holdout
  split is rejected under the public `tasks/` root by design.
- Promote pilot-proven tasks from `candidate` to `regression` maturity so they
  count in headline results, and retire tasks the item analysis flags.

## Grading and calibration

- Run held-out, human-labelled calibration for the Jev-with-LLM-fallback
  policy; offline fixtures prove transport and verdict propagation, not judge
  quality.
- Pin and record the judge model, endpoint and policy hash for every published
  comparison. When a policy changes, keep previous judgments unchanged and
  write new judgments under the new policy id and hash, so old and new scores
  are never compared as though they used the same evaluator.

## Non-goals

- No heuristic quality grader. Semantic grading stays Jev-first with an LLM
  fallback; missing judges leave trials `INVALID` or `ungraded` rather than
  inventing a score.
- No cost or time caps. The meter records every request and refuses none for
  spend; the product under test owns its limits.
