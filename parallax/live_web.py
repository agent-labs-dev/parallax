"""The live-web suite: browser tasks on public websites, graded by `browser-evidence-v2`.

The tasks were written for fastbrowse's release benchmark and moved here so every browser agent is measured on a
suite none of them owns. Their ids and versions are the ones fastbrowse published, so earlier rows still join.
Each task names where it starts, what to do and whether it authorizes irreversible actions; a login task names the
environment variables holding the practice site's published demo login.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from parallax.models import Area, Capability, JsonObject, Maturity, Model, Provenance, TaskRecord

DATA = Path(__file__).with_name("live_web.json")


class Portion(StrEnum):
    """Which part of the suite a task belongs to: `core` is the scored set, `dev` and `stretch-dev` are held for
    development, and `sweep` tasks are diagnostics no harness is expected to pass yet."""

    CORE = "core"
    DEV = "dev"
    STRETCH_DEV = "stretch-dev"
    SWEEP = "sweep"


class LiveWebTask(Model):
    id: str
    version: int
    suite: Portion
    category: str
    start_url: str
    task: str
    authorizes: bool
    secrets: dict[str, str] = {}
    answer_schema: JsonObject | None = None


class LiveWebSuite(Model):
    tasks: tuple[LiveWebTask, ...]


def record(task: LiveWebTask) -> TaskRecord:
    requires = {Capability.BROWSER} | ({Capability.CREDENTIALS} if task.secrets else set())
    return TaskRecord(
        id=task.id,
        version=task.version,
        area=Area.BROWSER,
        summary=task.task,
        prompt=f"Start at {task.start_url}. {task.task}",
        grading="browser-evidence-v2",
        requires=frozenset(requires),
        authorizes=task.authorizes,
        maturity=Maturity.ASPIRATIONAL if task.suite is Portion.SWEEP else Maturity.CANDIDATE,
        provenance=Provenance.HAND_AUTHORED,
        secrets=task.secrets,
        answer_schema=task.answer_schema,
        data={"start_url": task.start_url, "category": task.category, "suite": task.suite.value},
    )


def task_records() -> list[TaskRecord]:
    """The `parallax.task_sources` entry point `live-web`."""
    return [record(task) for task in LiveWebSuite.model_validate_json(DATA.read_text()).tasks]
