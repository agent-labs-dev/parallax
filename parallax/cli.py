"""`parallax` command line: run, show, compare, upload --resume, check and ci init.

Exit codes: 0 ok; 1 a failed check, a run with INVALID > 10%, or a regression under
--fail-on-regression; 2 usage error; 3 environment (docker, world boot, or Langfuse).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from parallax import __version__
from parallax.box import BoxError
from parallax.harnesses.base import HarnessError
from parallax.judge import JudgeConfigError
from parallax.models import Isolation, Task, TrialResult, Verdict
from parallax.task import SelectError, TaskError, discover, lint, lint_suites, rebind_persona, resolve, suite_roots
from parallax.track.projection import DEFAULT_DATASET
from parallax.track.sink import LANGFUSE_ERRORS, TrackError

if TYPE_CHECKING:
    from parallax.track.compare import RunRecord

OK, FAILED, USAGE, ENVIRONMENT = 0, 1, 2, 3
RUNS = Path("runs")
logger = logging.getLogger("parallax")


class UsageError(Exception):
    """A bad invocation: exit 2 with the message."""


def csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def pairs(values: Sequence[str], flag: str) -> list[tuple[str, str]]:
    """`KEY=VALUE` arguments; the value may hold `=`, the key never does."""
    out: list[tuple[str, str]] = []
    for value in values:
        key, sep, rest = value.partition("=")
        if not (sep and key and rest):
            raise UsageError(f"{flag} takes KEY=VALUE, got {value!r}")
        out.append((key, rest))
    return out


def baseline_args(values: Sequence[str], runs: Sequence[RunRecord]) -> tuple[dict[str, str], list[RunRecord]]:
    """`--baseline` split into the metadata filters and the runs they name.

    A value that names an exact run (its id, experiment name or `run_id`) is a reference, even when it
    holds `=` (an `--out` directory name can). Any other value with `=` is a metadata filter `KEY=VALUE`.
    """
    where: dict[str, str] = {}
    named: list[RunRecord] = []
    for value in values:
        hits = [r for r in runs if value in (r.name, r.id, r.metadata.get("run_id"))]
        if hits:
            named.extend(hits)
            continue
        key, sep, rest = value.partition("=")
        if not sep:
            raise UsageError(f"--baseline {value!r} matched no run")
        if not (key and rest):
            raise UsageError(f"--baseline takes KEY=VALUE or a run id, got {value!r}")
        where[key] = rest
    return where, named


def tasks_for(terms: Sequence[str], extra_roots: Sequence[Path]) -> list[Task]:
    tasks = resolve(list(terms), extra_roots)
    if not tasks:
        raise UsageError(f"SELECT {' '.join(terms) or '(all)'} matched no tasks")
    return tasks


def summary(results: Sequence[TrialResult]) -> str:
    counts = Counter(r.verdict for r in results)
    return ", ".join(f"{v.value}={counts[v]}" for v in Verdict if counts[v]) or "no trials"


# ---------------------------------------------------------------------------
# run / upload
# ---------------------------------------------------------------------------


def cmd_run(a: argparse.Namespace) -> int:
    from parallax.provenance import check_caller_keys
    from parallax.runner import INVALID_EXIT_SHARE, RunOptions, default_out, invalid_share, latest_results, run
    from parallax.track.projection import check_meta
    from parallax.track.sink import connect
    from parallax.track.upload import SEAL, seal

    # Resolved so resuming via a symlink keeps the real run id (and docker network name).
    out: Path = a.resume.resolve() if a.resume else a.out or default_out()
    if a.resume and not (out / "manifest.json").is_file():
        raise UsageError(f"--resume {a.resume}: not a run directory (no manifest.json)")
    if a.resume and (out / SEAL).is_file():
        raise UsageError(
            f"--resume {a.resume}: the run is finished and waiting to upload; use parallax upload --resume"
        )
    if a.world_port is not None and a.isolation is not Isolation.REMOTE:
        raise UsageError("--world-port pins the world server for a tunnel: it needs --isolation remote")
    tasks = tasks_for(a.select, a.tasks)
    harnesses = csv(a.harness)
    models: tuple[str | None, ...] = csv(a.model) if a.model else (None,)
    extra = dict(pairs(a.provenance, "--provenance"))
    pinned = tuple(pairs(a.pair, "--pair"))
    meta = dict(pairs(a.meta, "--meta"))
    check_meta(meta)
    try:
        check_caller_keys(extra)
    except ValueError as exc:
        raise UsageError(f"--provenance: {exc}") from exc
    if a.dry_run:
        print("\n".join(t.id for t in tasks))
        print(f"{len(tasks)} tasks x {len(harnesses)} harnesses x {len(models)} models x {a.repeats} repeats -> {out}")
        return OK
    # Refuse before spending anything: a run Langfuse cannot take has nowhere to go.
    connect(write=True)
    opts = RunOptions(
        out=out, repeats=a.repeats, isolation=a.isolation, concurrency=a.concurrency,
        world_public_url=a.world_public_url, world_port=a.world_port, judge=csv(a.judge) if a.judge else (),
        include_xfail=a.include_xfail, selection=" ".join(a.select), provenance_extra=extra,
        pairs=pinned, retry_invalid=a.retry_invalid, turn_timeout_s=a.turn_timeout, retry_wait_s=a.retry_wait,
        budget_usd=a.budget, reserve_usd=a.reserve,
    )  # fmt: skip
    run_dir = run(tasks, harnesses, models, opts)
    # Latest row per key: a transient-invalid trial's failed attempt is evidence, not a second trial.
    results = latest_results(run_dir / "results.jsonl")
    share = invalid_share(results)
    print(f"{run_dir.name}: {summary(results)}")
    seal(run_dir, dataset=a.dataset, note=a.note, meta=meta)
    if (status := upload_one(run_dir)) != OK:
        return status
    if share > INVALID_EXIT_SHARE:
        print(
            f"INVALID share {share:.0%} exceeds {INVALID_EXIT_SHARE:.0%}: the run measures the environment, not the agent"
        )
        return FAILED
    return OK


def upload_one(run_dir: Path) -> int:
    from parallax.track.upload import upload

    try:
        done = upload(run_dir)
    except LANGFUSE_ERRORS as exc:
        print(f"parallax: {exc}\nretry with: parallax upload --resume", file=sys.stderr)
        return ENVIRONMENT
    for name, _, url in done.experiments:
        print(f"  {name}\n    {url}")
    return OK


def cmd_upload(a: argparse.Namespace) -> int:
    from parallax.track.upload import pending

    waiting = pending(a.root)
    if not waiting:
        print(f"nothing pending under {a.root}")
        return OK
    return max(upload_one(run_dir) for run_dir in waiting)


# ---------------------------------------------------------------------------
# show / compare
# ---------------------------------------------------------------------------


def cmd_show(a: argparse.Namespace) -> int:
    from parallax.track.sink import connect, find_runs, list_runs, load_tasks

    client, _ = connect(write=False)
    runs = list_runs(client, a.dataset)
    if a.run is None:
        for run in runs[: a.limit]:
            scores = ", ".join(f"{k} {run.scores[k]:.0%}" for k in ("pass_at_1", "pass_all_repeats") if k in run.scores)
            print(f"{run.started_at:%Y-%m-%d %H:%M}  {run.row.replace('|', '/'):40}  {scores:38}  {run.name}")
        return OK
    for run in find_runs(runs, a.run):
        loaded = load_tasks(client, run)
        print(f"{run.name}  ({run.id})")
        for key, task in sorted(loaded.tasks.items()):
            cost = "unknown" if task.cost_usd is None else f"{task.cost_usd:.4f} USD"
            print(f"  {key:40} {' '.join(f'{v:.0f}' for v in task.verdicts) or 'no valid repeat':12} {cost}")
    return OK


def cmd_report(a: argparse.Namespace) -> int:
    from parallax.models import Verdict
    from parallax.track.projection import ItemInput, ItemOutput
    from parallax.track.report import evidence_reasons, render
    from parallax.track.sink import EVIDENCE, connect, decode, experiment_items, find_runs, list_runs, media_bytes

    client, project = connect(write=False)
    links = pairs(a.link, "--link")
    for run in find_runs(list_runs(client, a.dataset), a.run):
        items, reasons = [], {}
        for item in experiment_items(client, run.id, "core,io"):
            output = decode(item.output)
            task, result = ItemInput.model_validate(decode(item.input)), ItemOutput.model_validate(output)
            items.append((task, result))
            invalid = any(r.verdict is Verdict.INVALID and r.invalid_reason_sha256 for r in result.repeats)
            evidence = (
                media_bytes(client, output.get(EVIDENCE))
                if a.reasons and invalid and isinstance(output, dict)
                else None
            )
            if evidence is not None:
                reasons |= evidence_reasons(evidence)
        print(render(run, items, [("Langfuse experiment", project.experiment_url(run.id)), *links], reasons))
    return OK


def cmd_compare(a: argparse.Namespace) -> int:
    from parallax.stats import MixedJudgeIdentities
    from parallax.track.compare import (
        Direction,
        RowComparison,
        baseline_for,
        compare_rows,
        identity_of,
        render,
        validity_conflict,
    )
    from parallax.track.rows import identity_conflicts
    from parallax.track.sink import connect, find_runs, list_runs, load_tasks

    client, project_ = connect(write=False)
    runs = list_runs(client, a.dataset)
    if not runs:
        raise TrackError("nothing is recorded yet; `parallax run` records a run")
    where, named = baseline_args(a.baseline, runs)
    notes: list[str] = []
    results: list[RowComparison] = []
    for cand in find_runs(runs, a.candidate or runs[0].metadata["run_id"]):
        row = cand.row.replace("|", "/")
        before = baseline_for(runs, cand, where, named=named)
        if before is None:
            notes.append(f"{row}: no baseline run to compare against")
            continue
        if (invalid := validity_conflict(before, cand)) is not None:
            notes.append(f"{row}: not compared: {invalid}")
            continue
        before, cand = load_tasks(client, before), load_tasks(client, cand)
        conflicts = identity_conflicts(identity_of(before), identity_of(cand))
        if conflicts and not a.allow_identity_change:
            notes.append(f"{row}: not compared: {'; '.join(conflicts)} (--allow-identity-change compares anyway)")
            continue
        try:
            comparison = compare_rows(before, cand, seed=a.seed)
        except MixedJudgeIdentities as exc:
            notes.append(f"{row}: not compared: {exc}")
            continue
        if comparison is None:
            notes.append(f"{row}: {before.name} and {cand.name} share no task at the same version")
            continue
        results.append(comparison)
        notes.append(f"{row}: [side by side in Langfuse]({project_.experiment_url(cand.id, against=before.id)})")
    print("\n\n".join([*([render(results)] if results else []), "\n".join(notes)]))
    regressed = any(c.direction is Direction.REGRESSED for c in results)
    # A gate that could not compare a row has not shown that row is free of regressions.
    uncompared = len(notes) > len(results)
    return FAILED if a.fail_on_regression and (regressed or uncompared or not results) else OK


# ---------------------------------------------------------------------------
# check / ci
# ---------------------------------------------------------------------------


def cmd_check(a: argparse.Namespace) -> int:
    from parallax.controls import check, render

    tasks = tasks_for(a.select, a.tasks)
    problems = [(t.id, p) for t in tasks for p in lint(t)]
    suites = suite_roots()
    problems += [("suites", p) for p in lint_suites(discover(r for rs in suites.values() for r in rs), suites)]
    for task_id, p in problems:
        print(f"{task_id}: {p}")
    print(f"{len(tasks)} tasks, {len(problems)} problems")
    if problems or a.lint_only:
        return FAILED if problems else OK
    if a.account is not None:
        tasks = [rebind_persona(t, a.account) for t in tasks]
    report = check(tasks, a.isolation)
    print(render(report))
    return OK if report.ok else FAILED


def cmd_ci_init(a: argparse.Namespace) -> int:
    from parallax.ci import write_workflow

    path = write_workflow(
        a.path, suite=a.suite, harness=a.harness, model=a.model, repeats=a.repeats, install=a.install,
        isolation=a.isolation.value, branch=a.branch, force=a.force, runner=a.runner,
        dispatch_runner=a.dispatch_runner, from_checkout=a.from_checkout,
    )  # fmt: skip
    print(f"wrote {path}\nnext: set the repository variables and secrets it names, then open a pull request")
    return OK


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def add_select(p: argparse.ArgumentParser) -> None:
    p.add_argument("select", nargs="*", metavar="SELECT", help="suites, task ids/globs/paths, filters")
    p.add_argument("--tasks", type=Path, action="append", default=[], metavar="DIR", help="extra task root")


def add_dataset(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dataset", default=DEFAULT_DATASET, help="Langfuse dataset (default: %(default)s)")


def run_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("run", help="run tasks against harnesses and record the run in Langfuse")
    add_select(p)
    add_dataset(p)
    p.add_argument("--harness", required=True, help="H[,H]")
    p.add_argument("--model", help="M[,M]")
    p.add_argument(
        "--pair", action="append", default=[], metavar="TASK=MODEL",
        help="run only this task in this model (repeatable); default: every task in every model",
    )  # fmt: skip
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument(
        "--retry-invalid", type=int, default=1, metavar="N",
        help="re-run a trial that ended INVALID for a transient execution fault, up to N further attempts",
    )  # fmt: skip
    p.add_argument(
        "--retry-wait", type=float, default=0.0, metavar="SECONDS",
        help="wait this long before the first retry round, doubling each round (lets a site outage clear)",
    )  # fmt: skip
    p.add_argument(
        "--budget", type=float, metavar="USD",
        help="campaign spend limit across this run and its resumes: a trial starts only while its --reserve fits",
    )  # fmt: skip
    p.add_argument("--reserve", type=float, metavar="USD", help="what each trial reserves of --budget before it starts")
    p.add_argument("--turn-timeout", type=float, metavar="SECONDS", help="kill a turn still running after this")
    p.add_argument("--out", type=Path, help="run directory (default: runs/<timestamp>); deleted once uploaded")
    p.add_argument("--resume", type=Path, metavar="RUN_DIR", help="continue an interrupted run")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--isolation", type=Isolation, choices=list(Isolation), default=Isolation.DOCKER)
    p.add_argument("--world-public-url")
    p.add_argument(
        "--world-port", type=int, metavar="PORT", help="remote isolation: serve the world on this port (for a tunnel)"
    )
    p.add_argument("--judge", help="M[,M]")
    p.add_argument("--include-xfail", action="store_true")
    p.add_argument(
        "--provenance", action="append", default=[], metavar="KEY=VALUE",
        help="a value only the caller knows, for provenance.json: api_url or <plugin>.<key>",
    )  # fmt: skip
    p.add_argument("--note", default="", help="experiment description, e.g. what changed")
    p.add_argument("--meta", action="append", default=[], metavar="KEY=VALUE", help="extra experiment metadata")
    p.add_argument("--dry-run", action="store_true", help="list the tasks; run nothing")
    p.set_defaults(func=cmd_run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parallax", description="Evaluate agent harnesses against simulated worlds.")
    parser.add_argument("--version", action="version", version=f"parallax {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser(sub)

    p = sub.add_parser("show", help="recorded runs, newest first, or one run task by task")
    p.add_argument("run", nargs="?", help="run id or experiment name")
    p.add_argument("--limit", type=int, default=20)
    add_dataset(p)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("report", help="one run as Markdown: its tally and every trial that did not pass, with why")
    p.add_argument("run", help="run id or experiment name")
    p.add_argument(
        "--reasons", action="store_true",
        help="quote invalid trials' reasons from their evidence; they can quote the agent's work, so keep it "
        "to reports only people who may read the evidence see",
    )  # fmt: skip
    p.add_argument("--link", action="append", default=[], metavar="LABEL=URL", help="a link under the report")
    add_dataset(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("compare", help="paired pass@1, cost and time deltas against a baseline run")
    p.add_argument("candidate", nargs="?", help="run id or experiment name (default: the newest run)")
    p.add_argument(
        "--baseline", action="append", default=[], metavar="RUN|KEY=VALUE",
        help="a run id or experiment name, or the newest earlier run whose metadata matches, e.g. branch=main "
        "(default: the previous run)",
    )  # fmt: skip
    p.add_argument(
        "--fail-on-regression", action="store_true",
        help="exit 1 when a row's 95%% CI is below zero, or a row could not be compared",
    )  # fmt: skip
    p.add_argument(
        "--allow-identity-change", action="store_true",
        help="pair runs that measured different things: judge policy, tasks, harness or Parallax revision",
    )  # fmt: skip
    p.add_argument("--seed", type=int, default=0, help="bootstrap seed")
    add_dataset(p)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("upload", help="finish uploads an earlier run could not complete")
    p.add_argument("--resume", action="store_true", required=True)
    p.add_argument("--root", type=Path, default=RUNS, help="where runs wait (default: %(default)s)")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("check", help="task lint, then oracle=1, null controls=0 and mutants killed")
    add_select(p)
    p.add_argument("--lint-only", action="store_true", help="lint the tasks; run no controls")
    p.add_argument("--isolation", type=Isolation, choices=[Isolation.HOST, Isolation.DOCKER], default=Isolation.HOST)
    p.add_argument(
        "--account",
        metavar="EMAIL",
        help="run the controls with the user signed in as EMAIL, as an AccountBound harness does",
    )
    p.set_defaults(func=cmd_check)

    ci = sub.add_parser("ci", help="continuous evaluation in GitHub Actions")
    csub = ci.add_subparsers(dest="ci_command", required=True)
    p = csub.add_parser("init", help="write a workflow that evaluates every pull request against main")
    p.add_argument("--suite", required=True, help="the SELECT each run evaluates")
    p.add_argument("--harness", required=True)
    p.add_argument("--model")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--install", default="", help="shell step that installs the harness on the runner")
    p.add_argument("--isolation", type=Isolation, choices=[Isolation.DOCKER, Isolation.HOST], default=Isolation.DOCKER)
    p.add_argument("--branch", default="main", help="the default branch, whose runs are the baseline")
    p.add_argument("--runner", default="ubuntu-24.04", help="runner label for push and pull request runs")
    p.add_argument("--dispatch-runner", help="default runner label for a dispatched campaign (default: --runner)")
    p.add_argument(
        "--from-checkout", action="store_true",
        help="run the checked-out commit's Parallax instead of this release (for Parallax's own repository)",
    )  # fmt: skip
    p.add_argument("--path", type=Path, default=Path(".github/workflows/parallax.yml"))
    p.add_argument("--force", action="store_true", help="replace an existing workflow")
    p.set_defaults(func=cmd_ci_init)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    func: Callable[[argparse.Namespace], int] = args.func
    try:
        return func(args)
    except (UsageError, TaskError, SelectError, JudgeConfigError, ValueError, FileExistsError) as exc:
        print(f"parallax: {exc}", file=sys.stderr)
        return USAGE
    except (BoxError, HarnessError, *LANGFUSE_ERRORS) as exc:
        print(f"parallax: environment: {exc}", file=sys.stderr)
        return ENVIRONMENT
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
