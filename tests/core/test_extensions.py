"""Task extensions validate plugin metadata blocks and impose lane rules; provenance sources add fields."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from parallax import extensions, plugins, provenance
from parallax.extensions import LaneRules, TaskExtension, check_lanes
from parallax.models import Capability, Isolation, Model, Task
from parallax.runner import RunOptions, run_async
from parallax.task import TaskError, lint, load_task, mounted_services

TASKS = Path(__file__).parent / "fixtures" / "tasks"
R1 = Path(__file__).parent / "fixtures" / "runs" / "r1"


class FakeBlock(Model):
    level: int


FAKE = TaskExtension(name="fake", model=FakeBlock, lane=LaneRules("Fake", harnesses=("remote",), concurrency_one=True))


@pytest.fixture
def fake_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extensions, "task_extensions", lambda: {"fake": FAKE})


def _task_with(tmp_path: Path, block: str) -> Path:
    root = tmp_path / "mini-mail"
    shutil.copytree(TASKS / "mini-mail", root)
    toml = root / "task.toml"
    toml.write_text(toml.read_text() + f"\n{block}\n")
    return root


def test_a_registered_block_is_validated_and_stored(tmp_path: Path, fake_registered: None) -> None:
    task = load_task(_task_with(tmp_path, "[metadata.fake]\nlevel = 2"))
    assert task.extensions == {"fake": {"level": 2}}
    assert "extensions" not in load_task(TASKS / "mini-mail").model_dump()


def test_a_bad_block_is_a_load_error(tmp_path: Path, fake_registered: None) -> None:
    with pytest.raises(TaskError, match="level"):
        load_task(_task_with(tmp_path, '[metadata.fake]\nlevel = "high"'))


def test_an_unknown_namespace_is_a_load_error(tmp_path: Path, fake_registered: None) -> None:
    with pytest.raises(TaskError, match="unknown task metadata namespace 'other'"):
        load_task(_task_with(tmp_path, "[metadata.other]\nlevel = 1"))


async def test_extension_lane_rules_block_a_disallowed_harness(tmp_path: Path, fake_registered: None) -> None:
    task = load_task(_task_with(tmp_path, "[metadata.fake]\nlevel = 1"))
    options = RunOptions(out=tmp_path / "r", isolation=Isolation.HOST, quiet=True)
    with pytest.raises(ValueError, match="^Fake collection requires only remote and --concurrency 1$"):
        await run_async([task], ["oracle"], [None], options)
    check_lanes([task], ["remote"], concurrency=1)


def _claiming(**fields: object) -> TaskExtension:
    return TaskExtension(name="fake", model=FakeBlock, core_table=True, **fields)  # type: ignore[arg-type]


def test_an_extension_can_claim_a_core_sub_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _task_with(tmp_path, "")
    toml = root / "task.toml"
    toml.write_text(
        toml.read_text().replace("[metadata.parallax]", "[metadata.parallax.fake]\nlevel = 3\n\n[metadata.parallax]", 1)
    )
    with pytest.raises(TaskError, match="fake"):
        load_task(root)  # unclaimed, it is an unknown core field
    monkeypatch.setattr(extensions, "task_extensions", lambda: {"fake": _claiming()})
    assert load_task(root).extensions == {"fake": {"level": 3}}


def test_an_extension_check_rejects_the_loaded_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def two_steps(task: Task) -> Iterator[str]:
        if len(task.turns) != 2:
            yield "exactly two steps"

    monkeypatch.setattr(
        extensions, "task_extensions", lambda: {"fake": TaskExtension("fake", FakeBlock, check=two_steps)}
    )
    root = _task_with(tmp_path, "[metadata.fake]\nlevel = 1")
    with pytest.raises(TaskError, match="metadata.fake: exactly two steps"):
        load_task(root)


@pytest.mark.parametrize("check_absent", [False, True])
def test_extension_checks_can_reject_missing_declarations(monkeypatch, check_absent):
    def requires_block(task):
        if "fake" not in task.extensions:
            yield "missing declaration"

    ext = TaskExtension("fake", FakeBlock, check=requires_block, check_absent=check_absent)
    monkeypatch.setattr(extensions, "task_extensions", lambda: {"fake": ext})
    if check_absent:
        with pytest.raises(TaskError, match="missing declaration"):
            load_task(TASKS / "mini-mail")
    else:
        load_task(TASKS / "mini-mail")


def test_an_extension_can_replace_the_default_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ext = TaskExtension("fake", FakeBlock, default_services=False)
    monkeypatch.setattr(extensions, "task_extensions", lambda: {"fake": ext})
    plain = load_task(TASKS / "mini-mail")
    assert Capability.MCP in plain.meta.requires
    owned = load_task(_task_with(tmp_path, "[metadata.fake]\nlevel = 1"))
    assert mounted_services(owned) == frozenset(owned.meta.services) < mounted_services(plain)


def test_only_a_real_account_extension_may_name_real_addresses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _task_with(tmp_path, "[metadata.fake]\nlevel = 1")
    (root / "notes.md").write_text("owner: someone@real-company.com\n")
    for real, problems in ((False, 1), (True, 0)):
        ext = TaskExtension("fake", FakeBlock, real_accounts=real)
        monkeypatch.setattr(extensions, "task_extensions", lambda ext=ext: {"fake": ext})
        found = [p for p in lint(load_task(root)) if "real-company.com" in p]
        assert len(found) == problems


def test_provenance_sources_are_merged_under_their_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = tmp_path / "run"
    run.mkdir()
    shutil.copy(R1 / "manifest.json", run / "manifest.json")
    assert "extra" not in provenance.collect(run, {}).model_dump()
    monkeypatch.setattr(provenance, "sources", lambda: {"fake": lambda run_dir: {"head": run_dir.name}})
    p = provenance.collect(run, {})
    assert p.extra == {"fake.head": "run"}
    provenance.write(run, p)
    assert provenance.read(run) == p
    monkeypatch.setattr(provenance, "sources", lambda: {"a.b": lambda _: {"c": "1"}, "a": lambda _: {"b.c": "2"}})
    with pytest.raises(ValueError, match="recorded twice"):
        provenance.collect(run, {})


@dataclass(frozen=True)
class FakeEntryPoint:
    name: str
    target: object
    value: str = "fake.module:obj"

    def load(self) -> object:
        return self.target


@pytest.fixture
def entry_points(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[FakeEntryPoint]]]:
    groups: dict[str, list[FakeEntryPoint]] = {}
    monkeypatch.setattr(plugins, "entry_points_in", lambda group: groups.get(group, []))
    extensions.task_extensions.cache_clear()
    yield groups
    extensions.task_extensions.cache_clear()


def test_the_loader_registers_an_extension_by_its_namespace(entry_points: dict[str, list[FakeEntryPoint]]) -> None:
    entry_points[extensions.ENTRY_POINT_GROUP] = [FakeEntryPoint("fake", FAKE)]
    assert extensions.task_extensions() == {"fake": FAKE}


@pytest.mark.parametrize(
    ("registered", "error"),
    [
        ([FakeEntryPoint("fake", object())], TypeError),
        ([FakeEntryPoint("other", FAKE)], ValueError),
        ([FakeEntryPoint("parallax", TaskExtension(name="parallax", model=FakeBlock))], ValueError),
        ([FakeEntryPoint("fake", FAKE), FakeEntryPoint("fake", FAKE)], ValueError),
    ],
    ids=["not-an-extension", "name-mismatch", "reserved", "repeated"],
)
def test_the_loader_rejects_a_bad_entry_point(
    entry_points: dict[str, list[FakeEntryPoint]], registered: list[FakeEntryPoint], error: type[Exception]
) -> None:
    entry_points[extensions.ENTRY_POINT_GROUP] = registered
    with pytest.raises(error):
        extensions.task_extensions()


def test_the_provenance_loader_rejects_a_repeated_source(entry_points: dict[str, list[FakeEntryPoint]]) -> None:
    source = FakeEntryPoint("fake", lambda _: {})
    entry_points[provenance.ENTRY_POINT_GROUP] = [source]
    assert provenance.sources() == {"fake": source.target}
    entry_points[provenance.ENTRY_POINT_GROUP] = [source, source]
    with pytest.raises(ValueError, match="registered twice"):
        provenance.sources()


def test_a_task_without_a_parallax_table_is_a_load_error(tmp_path: Path) -> None:
    root = tmp_path / "mini-mail"
    shutil.copytree(TASKS / "mini-mail", root)
    toml = root / "task.toml"
    toml.write_text(toml.read_text().replace("[metadata.parallax]", "[metadata.gone]"))
    with pytest.raises(TaskError, match=r"no \[metadata.parallax\] table"):
        load_task(root)
