from __future__ import annotations

import json
from pathlib import Path

from parallax.live_web import task_records
from parallax.models import Capability
from parallax.task import record_hash, source_tasks

LOCK = Path(__file__).parent / "fixtures" / "live_web_lock.json"


def test_a_live_web_task_changes_only_with_a_new_version() -> None:
    """Rows pool by task id and version, so a task whose content changes must bump its version. The lock then has to be
    regenerated, so the next unversioned edit is caught against the new baseline."""
    lock = json.loads(LOCK.read_text())
    current = {r.id: {"version": r.version, "hash": record_hash(r)} for r in task_records()}
    unbumped = sorted(
        task_id
        for task_id, entry in current.items()
        if task_id in lock and entry["hash"] != lock[task_id]["hash"] and entry["version"] == lock[task_id]["version"]
    )
    assert unbumped == [], f"changed without a version bump: {unbumped}"
    assert current == lock, "regenerate tests/core/fixtures/live_web_lock.json after a deliberate version bump"


def test_the_live_web_source_loads_through_its_entry_point() -> None:
    tasks = source_tasks(source_names=("live-web",))
    assert len(tasks) == len(json.loads(LOCK.read_text()))
    pause = next(task for task in tasks if task.id == "saucedemo-pause")
    assert pause.meta.authorizes is False and Capability.AUTHORIZATION in pause.meta.requires
