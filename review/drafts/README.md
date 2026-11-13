# Task drafts

A proposed task waits here until a maintainer promotes it. The loader never
discovers anything under `review/drafts/`, so a draft cannot affect a run.

## Proposal format

Each draft is one directory, `review/drafts/<task-id>/`. It uses the full task
layout from [docs/authoring.md](../../docs/authoring.md), with two differences:

- `maturity = "draft"` in `task.toml`;
- a `PROPOSAL.md` covering four things:
  - **Failure class.** Give the failure mode and area, and a real, anonymised
    description of the behaviour the task catches.
  - **Why no existing task covers it.** Name the nearest existing task ids.
  - **Negative twin.** Name the task in which the opposite behaviour is correct.
  - **Evidence.** State where the failure was seen. Describe it without customer
    data.

Drafts may come from people or from automated jobs.

## Promotion

A maintainer promotes a draft in four steps:

1. Rewrite any leftover real names or domains into synthetic ones.
2. Move the directory to `tasks/core/<task-id>/` and set
   `maturity = "candidate"`.
3. Run `uv run parallax check tasks/core/<task-id> --isolation host` until it
   prints `ok`.
4. Delete `PROPOSAL.md`, keeping its reasoning in the pull request description.

The task counts in headline results only after pilot item analysis moves it to `regression`
or `aspirational` (see [docs/methodology.md](../../docs/methodology.md#maturity-lifecycle)).
