# Troubleshooting

First-run failures and what to do about them. The message printed just before
the exit names the cause; the [exit code](quickstart.md#exit-codes) says which
kind it is.

## The run stops before executing anything

`parallax run` confirms Langfuse accepts writes before it spends anything, and
exits 3 when it does not:

- **Missing credentials.** Set `LANGFUSE_BASE_URL` (or `LANGFUSE_HOST`),
  `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY`.
- **No project named.** Writes need `PARALLAX_LANGFUSE_PROJECT`.
- **Wrong project.** The keys belong to a project other than
  `PARALLAX_LANGFUSE_PROJECT`. Parallax refuses rather than write eval data into
  another project, such as a production tracing project.

`--dry-run` lists the selected tasks without contacting Langfuse.

## The upload failed

The run finished but Langfuse did not take all of it, or the read-back did not
match. The run directory stays under `runs/` and the run exits 3. Fix the
cause, then:

```sh
parallax upload --resume
```

It finishes every pending run under `runs/` (`--root DIR` for another place).
Writes land on fixed ids, so a retry never duplicates anything. A run
directory is deleted only after Langfuse provably holds all of it.

## Trials come back `INVALID`

`INVALID` is an evidence or execution fault, never a quality verdict. Common
causes, with the reason in the row or on stderr:

- **Missing judges.** No `OPENROUTER_API_KEY` for Jev leaves checks ungraded.
  See [judging](jev-judge.md).
- **`boot_error` / `box:` / `start:`.** The harness could not boot. The reason
  names the missing binary, image or configuration.
- **`meter unreachable:`.** A metered harness needs the meter, which needs the
  upstream key for the wire it uses (`PARALLAX_UPSTREAM_OPENAI_KEY` or
  `PARALLAX_UPSTREAM_ANTHROPIC_KEY`).

A transient fault is retried inside the run (`--retry-invalid`, default 1). A
run whose `INVALID` share still exceeds 10% is uploaded, then exits 1: it
measured the environment, not the agent. Fix the environment rather than
reading the numbers.

## A trial is `ungraded`

The trial executed, but a judge could not settle a check: the fallback judges
disagreed or returned nothing. The known decisions and judge charges are kept.
See [judging](jev-judge.md).

## Docker errors, or no Docker

Harnesses default to `--isolation docker`. For development, `--isolation host`
runs a harness on your machine with native web tools disabled by flag; it is
not a grading boundary (see [methodology](methodology.md#web-isolation)).
`parallax check` defaults to host isolation.

## `unknown harness X`

The error lists every known name. Built-in manifests, then entry points, then
the controls resolve in that order. A plugin harness appears only after its
package is installed into the same environment as Parallax.

## `compare` says a row was not compared

The reason follows on the same line:

- **no baseline run**: nothing earlier of that harness/model row matches
  `--baseline` and is verified. Record a baseline run first, with `--meta` set to
  match. A run counts only once its upload has proven Langfuse holds all of it, so
  a run whose upload was killed partway is skipped until `parallax upload --resume`
  finishes it.
- **different identity**: the runs differ in judge policy, task versions,
  harness version or config, or Parallax revision. See
  [versioning](tracking.md#versioning).
- **invalid share**: one of the runs has more than 10% INVALID trials.

## Usage errors

- `--world-port` pins the world server for a tunnel and requires
  `--isolation remote`.
- `--resume RUN_DIR` requires a directory containing `manifest.json`.
- `--pair TASK=MODEL` must match a planned trial exactly; a typo is an error,
  never silently a smaller run.
- `ci init` keeps an existing workflow unless `--force` is given.

## `check --lint-only` reports problems

It lints tasks and suite manifests without running anything and prints one line
per problem. `0 problems` means the selection parses and satisfies the
structural rules; it does not grade quality. CI runs
`parallax check core conformance --lint-only` on every change.

## Wrong Python

Parallax requires Python 3.13 or newer. `uv tool install` picks a matching
interpreter; `uv python list` shows what is available if installation refuses.
