"""`parallax report`: one recorded run as Markdown, for a step summary, a pull request or an issue.

It reads the run back from Langfuse, the same records `parallax show` and `parallax compare` use,
and lists every trial that did not pass with why: the checks that failed, or the trial's status
when it was invalid. Langfuse keeps only a hash of an invalid reason, because the text can quote
the agent's work; `--reasons` reads the text from the item's evidence zip, matched to that hash,
for a repository where everyone who can read the report may read the evidence.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from parallax.models import Verdict
from parallax.track.compare import RunRecord
from parallax.track.projection import ItemInput, ItemOutput, RepeatDetail

RESULTS = "results.jsonl"  # the item's result rows inside its evidence zip (upload.Evidence)
REASON_CHARS = 300


@dataclass(frozen=True)
class Trial:
    task_id: str
    task_version: int
    repeat: RepeatDetail

    @property
    def task(self) -> str:
        return f"{self.task_id}@v{self.task_version}"


def trials_of(items: Sequence[tuple[ItemInput, ItemOutput]]) -> list[Trial]:
    return [
        Trial(task.task_id, task.task_version, repeat)
        for task, output in sorted(items, key=lambda item: (item[0].task_id, item[0].task_version))
        for repeat in sorted(output.repeats, key=lambda r: r.repeat)
    ]


def evidence_reasons(zip_bytes: bytes) -> dict[str, str]:
    """Every invalid reason in an evidence zip's result rows, by the hash Langfuse records for it."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        if RESULTS not in archive.namelist():
            return {}
        lines = archive.read(RESULTS).decode().splitlines()
    reasons = (json.loads(line).get("invalid_reason") for line in lines if line.strip())
    return {hashlib.sha256(r.encode()).hexdigest(): r for r in reasons if r}


def cell(text: str) -> str:
    flat = " ".join(text.split()).replace("|", "\\|")
    return flat if len(flat) <= REASON_CHARS else flat[: REASON_CHARS - 1] + "…"


def why(repeat: RepeatDetail, reasons: Mapping[str, str]) -> str:
    if repeat.verdict is Verdict.NA:
        return "not applicable to this harness"
    if repeat.verdict is Verdict.UNGRADED:
        return "not graded"
    if repeat.verdict is Verdict.INVALID:
        status = repeat.status.value if repeat.status is not None else "no status"
        reason = repeat.invalid_reason or reasons.get(repeat.invalid_reason_sha256 or "")
        return cell(f"{status}: {reason}" if reason else status)
    failed = [c.id for c in repeat.checks or () if c.passed is False and not c.advisory]
    return "failed " + ", ".join(f"`{c}`" for c in failed) if failed else "no failing check recorded"


def render(
    run: RunRecord,
    items: Sequence[tuple[ItemInput, ItemOutput]],
    links: Iterable[tuple[str, str]],
    reasons: Mapping[str, str] | None = None,
) -> str:
    trials = trials_of(items)
    counts = Counter(t.repeat.verdict for t in trials)
    tally = ", ".join(f"{counts[v]} {v.value}" for v in Verdict if counts[v]) or "no trials"
    known = [t.repeat.cost_usd for t in trials if t.repeat.cost_usd is not None]
    unknown = len(trials) - len(known)
    if not known:
        cost = "cost unknown"
    else:
        cost = f"{sum(known):.2f} USD" + (f", cost unknown for {unknown} of them" if unknown else "")
    lines = [
        f"#### Results: `{run.metadata.get('run_id', run.name)}` ({run.row.replace('|', '/')})",
        "",
        f"**{tally}** of {len(trials)} trials over {len(items)} tasks, {cost}.",
        "",
    ]
    if missed := [t for t in trials if t.repeat.verdict is not Verdict.PASS]:
        lines += ["| Task | Trial | Result | Why |", "| --- | --- | --- | --- |"]
        lines += [
            f"| {t.task} | #{t.repeat.repeat} | {t.repeat.verdict.value} | {why(t.repeat, reasons or {})} |"
            for t in missed
        ]
        lines.append("")
    lines.append(" · ".join(f"[{label}]({url})" for label, url in links))
    return "\n".join(lines) + "\n"
