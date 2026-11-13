from __future__ import annotations

import runpy
import shutil
from pathlib import Path

import parallax.task as task_module
import pytest
from parallax.models import ApprovalDecision, Area, Capability, Maturity, RunOrigin
from parallax.task import (
    SelectError,
    TaskError,
    discover,
    lint,
    lint_suites,
    load_task,
    members,
    parse_select,
    rebind_persona,
    rebind_value,
    select,
    suite_manifest,
    task_hash,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tasks"
NO_SUITES: dict[str, tuple[Path, ...]] = {}


def copy_task(tmp_path: Path, name: str) -> Path:
    dest = tmp_path / name
    shutil.copytree(FIXTURES / name, dest)
    return dest


def set_meta(root: Path, old: str, new: str) -> None:
    toml = root / "task.toml"
    text = toml.read_text()
    assert old in text
    toml.write_text(text.replace(old, new))


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def test_single_turn_task_loads_with_canary_stripped_and_seed_folded() -> None:
    task = load_task(FIXTURES / "mini-mail")
    assert task.id == "mini-mail"
    assert task.oracle == "yaml"
    assert [t.name for t in task.turns] == ["turn-01"]
    assert "canary" not in task.turns[0].text
    assert task.turns[0].text.endswith("Vendor B.\n")
    assert set(task.seed.services) == {"mail", "contacts"}
    assert task.seed.services["mail"]["messages"][0]["id"] == "m1"
    assert [m.id for m in task.mutations.mutations] == ["never-sent", "archives-lunch", "double-send"]


def test_multi_step_task_resolves_turns_and_patches() -> None:
    task = load_task(FIXTURES / "mini-steps")
    first, second = task.turns
    assert (first.index, second.index) == (1, 2)
    assert first.patch is None
    assert second.patch is not None
    assert second.patch.insert["mail.messages"][0]["id"] == "m9"
    assert not second.new_session


def test_workspace_task_uses_scripted_oracle() -> None:
    assert load_task(FIXTURES / "mini-repo").oracle == "py"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: (r / "instruction.md").unlink(), "instruction.md"),
        (lambda r: (r / "solution" / "solve.py").write_text(""), "exactly one"),
        (lambda r: (r / "tests" / "checks.yaml").write_text("checks: []\n"), "semantic gate"),
        (lambda r: (r / "world" / "seed.yaml").write_text("services: {}\n"), "top level"),
    ],
)
def test_broken_task_raises_task_error(tmp_path: Path, mutate, message: str) -> None:
    root = copy_task(tmp_path, "mini-mail")
    mutate(root)
    with pytest.raises(TaskError, match=message):
        load_task(root)


def test_holdout_is_rejected_inside_the_repo_and_accepted_outside(tmp_path: Path) -> None:
    outside = copy_task(tmp_path, "mini-mail")
    set_meta(outside, "version = 1\n", 'version = 1\nsplit = "holdout"\n')
    assert load_task(outside).meta.split == "holdout"

    inside = FIXTURES.parent / "holdout-probe"
    shutil.copytree(outside, inside / "mini-mail")
    try:
        with pytest.raises(TaskError, match="holdout"):
            load_task(inside / "mini-mail")
    finally:
        shutil.rmtree(inside)


def test_task_hash_tracks_content_and_ignores_bytecode(tmp_path: Path) -> None:
    root = copy_task(tmp_path, "mini-repo")
    before = task_hash(root)
    (root / "solution" / "__pycache__").mkdir()
    (root / "solution" / "__pycache__" / "solve.cpython-313.pyc").write_bytes(b"\0")
    assert task_hash(root) == before
    (root / "environment" / "files" / "app.py").write_text("def total(items):\n    return sum(items)\n")
    assert task_hash(root) != before


# ---------------------------------------------------------------------------
# discovery and SELECT
# ---------------------------------------------------------------------------


def test_discover_sorts_by_id_and_rejects_duplicates(tmp_path: Path) -> None:
    assert [t.id for t in discover([FIXTURES])] == ["mini-mail", "mini-repo", "mini-steps"]
    copy_task(tmp_path / "a", "mini-mail")
    copy_task(tmp_path / "b", "mini-mail")
    with pytest.raises(TaskError, match="duplicate task id"):
        discover([tmp_path])


@pytest.fixture
def tasks() -> list:
    return discover([FIXTURES])


def ids(selected: list) -> list[str]:
    return [t.id for t in selected]


def test_select_unions_names_and_intersects_filters(tasks: list) -> None:
    assert ids(select(tasks, "mini-m*", NO_SUITES)) == ["mini-mail"]
    assert ids(select(tasks, ["mini-mail mini-repo"], NO_SUITES)) == ["mini-mail", "mini-repo"]
    assert ids(select(tasks, "area=code", NO_SUITES)) == ["mini-repo"]
    assert ids(select(tasks, "mini-* requires<=mcp,multi_turn", NO_SUITES)) == ["mini-mail", "mini-steps"]


def selection_suite(tmp_path: Path, *ids: str, within: str = "core") -> Path:
    root = tmp_path / "lite"
    root.mkdir()
    listed = ", ".join(f'"{i}"' for i in ids)
    (root / "suite.toml").write_text(f'owner = "public"\npurpose = "p"\nwithin = "{within}"\ntasks = [{listed}]\n')
    return root


def test_select_suites_selections_and_paths(tasks: list, tmp_path: Path) -> None:
    suites = {"core": (FIXTURES,), "lite": (selection_suite(tmp_path, "mini-steps"),)}
    assert ids(select(tasks, "core", suites)) == ["mini-mail", "mini-repo", "mini-steps"]
    assert ids(select(tasks, "lite", suites)) == ["mini-steps"]
    assert ids(select(tasks, str(FIXTURES / "mini-steps"), NO_SUITES)) == ["mini-steps"]


def test_lint_suites_names_unknown_ids_and_sources(tasks: list, tmp_path: Path) -> None:
    suites = {"core": (FIXTURES,), "lite": (selection_suite(tmp_path, "mini-steps", "gone"),)}
    assert lint_suites(tasks, suites) == ["suite lite: 'gone' is not a task in core"]
    assert lint_suites(tasks, {"lite": suites["lite"]}) == ["suite lite: within='core' is not a directory suite"]


def test_suite_without_manifest_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(TaskError, match="suite.toml"):
        suite_manifest((tmp_path,))


def test_builtin_members_are_empty_without_a_tasks_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_module, "TASKS_DIR", tmp_path)  # an installation with no bundled corpus
    assert members("lite") == frozenset()


def test_draft_under_a_given_root_is_selectable(tmp_path: Path) -> None:
    drafts, suite = tmp_path / "drafts", tmp_path / "suite"
    for root in (drafts, suite):
        set_meta(copy_task(root, "mini-mail"), "version = 1\n", 'version = 1\nmaturity = "draft"\n')
    (suite / "suite.toml").write_text('owner = "public"\npurpose = "p"\n')
    assert ids(select(discover([drafts]), "mini-mail", NO_SUITES)) == ["mini-mail"]
    assert select(discover([suite]), "mini-mail", {"suite": (suite,)}) == []  # a draft inside a suite stays hidden


def test_select_hides_retired_unless_named(tmp_path: Path) -> None:
    root = copy_task(tmp_path, "mini-mail")
    set_meta(root, "version = 1\n", 'version = 1\nmaturity = "retired"\n')
    tasks = discover([tmp_path])
    assert select(tasks, "", NO_SUITES) == []
    assert ids(select(tasks, "maturity=retired", NO_SUITES)) == ["mini-mail"]


@pytest.mark.parametrize("terms", ["nothing-like-this", "area=nope", "colour=red", "area=saas area=code", "area<=saas"])
def test_select_errors_are_loud(tasks: list, terms: str) -> None:
    with pytest.raises(SelectError):
        select(tasks, terms, NO_SUITES)


def test_parse_select_types_filters() -> None:
    sel = parse_select("core area=saas,code maturity=regression requires<=mcp")
    assert sel.names == ("core",)
    assert sel.areas == {Area.SAAS, Area.CODE}
    assert sel.maturities == {Maturity.REGRESSION}
    assert sel.requires_within == {Capability.MCP}


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["mini-mail", "mini-steps", "mini-repo"])
def test_fixtures_lint_clean(name: str) -> None:
    assert lint(load_task(FIXTURES / name)) == []


def lint_after(tmp_path: Path, rel: str, old: str, new: str, name: str = "mini-mail") -> list[str]:
    root = copy_task(tmp_path, name)
    path = root / rel
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))
    return lint(load_task(root))


@pytest.mark.parametrize(
    ("rel", "old", "new", "expected"),
    [
        ("instruction.md", "Vendor B.", "Vendor B. Contact ops@northwind.com.", "must use a .test"),
        ("instruction.md", "Vendor B.", "Vendor B. Key " + "sk-" + "abcdefghijklmnopqrstuv", "looks like a secret"),
        (
            "instruction.md",
            "<!-- parallax canary 3f2b8c1e-9a4d-4e7b-8c21-5d6f7a8b9c0d -->",
            "",
            "must end with an HTML comment",
        ),
        ("tests/mutations.yaml", "must_fail: [other-mail-untouched]", "must_fail: [ghost]", "names 'ghost'"),
        ("tests/checks.yaml", "must: [vendor b]", "must: [vendor b, true]", "text matcher"),
        ("tests/checks.yaml", "{contains: vendor b}", "{regex: '(unclosed'}", "bad regex"),
    ],
)
def test_lint_reports_planted_problem(tmp_path: Path, rel: str, old: str, new: str, expected: str) -> None:
    problems = lint_after(tmp_path, rel, old, new)
    assert any(expected in p for p in problems), problems


def test_lint_requires_multi_turn_for_continued_steps(tmp_path: Path) -> None:
    problems = lint_after(tmp_path, "task.toml", '["mcp", "multi_turn"]', '["mcp"]', name="mini-steps")
    assert any("multi_turn" in p for p in problems), problems


def test_lint_requires_id_to_match_directory(tmp_path: Path) -> None:
    root = copy_task(tmp_path, "mini-mail")
    renamed = root.rename(tmp_path / "other-name")
    assert any("directory name" in p for p in lint(load_task(renamed)))


@pytest.mark.parametrize(
    ("requires", "apps", "problem"),
    [
        ('["mcp", "workspace", "desktop"]', None, "must be declared together"),
        ('["mcp", "workspace"]', '{path="app.py", title="App"}', "must be declared together"),
        ('["mcp", "desktop"]', '{path="app.py", title="App"}', "desktop apps need"),
        ('["mcp", "workspace", "desktop"]', '{path="../app.py", title="App"}', "must stay under"),
        ('["mcp", "workspace", "desktop"]', '{path="missing.py", title="App"}', "must be a seeded Python file"),
    ],
)
def test_desktop_declarations_reject_unlaunchable_tasks(
    tmp_path: Path, requires: str, apps: str | None, problem: str
) -> None:
    root = copy_task(tmp_path, "mini-repo")
    import re

    path = root / "task.toml"
    content = re.sub(r"requires = .*", f"requires = {requires}", path.read_text())
    if apps is not None:
        content += f"\n[metadata.parallax.desktop]\napps = [{apps}]\n"
    path.write_text(content)
    assert any(problem in finding for finding in lint(load_task(root)))


def test_job_steps_load_their_origin_and_scripted_approval(tmp_path: Path) -> None:
    root = copy_task(tmp_path, "mini-steps")
    toml = root / "task.toml"
    toml.write_text(
        toml.read_text()
        .replace('["mcp", "multi_turn"]', '["mcp", "multi_turn", "unattended"]')
        .replace('patch = "sam-joins"', 'patch = "sam-joins"\nsession = "new"\norigin = "job"\napproval = "deny"')
    )
    task = load_task(root)
    assert [(t.origin, t.approval) for t in task.turns] == [
        (RunOrigin.MEMBER, None),
        (RunOrigin.JOB, ApprovalDecision.DENY),
    ]
    assert lint(task) == []


@pytest.mark.parametrize(
    ("step", "requires", "expected"),
    [
        ('session = "new"\norigin = "job"', '["mcp", "multi_turn"]', "include unattended"),
        ('approval = "allow_once"', '["mcp", "multi_turn", "unattended"]', 'set origin = "job"'),
        ('origin = "job"', '["mcp", "multi_turn", "unattended"]', 'session = "new"'),
    ],
)
def test_lint_rejects_malformed_job_steps(tmp_path: Path, step: str, requires: str, expected: str) -> None:
    root = copy_task(tmp_path, "mini-steps")
    toml = root / "task.toml"
    toml.write_text(
        toml.read_text()
        .replace('["mcp", "multi_turn"]', requires)
        .replace('patch = "sam-joins"', f'patch = "sam-joins"\n{step}')
    )
    problems = lint(load_task(root))
    assert any(expected in p for p in problems), problems


def test_installed_tasks_take_precedence_over_adjacent_directory(tmp_path: Path) -> None:
    package = tmp_path / "parallax"
    bundled = package / "tasks"
    bundled.mkdir(parents=True)
    (tmp_path / "tasks").mkdir()
    source = package / "task.py"
    shutil.copyfile(task_module.__file__, source)
    assert runpy.run_path(str(source))["TASKS_DIR"] == bundled


def test_select_rejects_missing_manifest_members(tasks: list, tmp_path: Path) -> None:
    suites = {"core": (FIXTURES,), "lite": (selection_suite(tmp_path, "mini-steps", "gone"),)}
    with pytest.raises(SelectError, match="gone"):
        select(tasks, "lite", suites)


@pytest.mark.parametrize("field", ["lite", "hard", "unknown_suite"])
def test_legacy_suite_compatibility_does_not_accept_invalid_metadata(tmp_path: Path, field: str) -> None:
    root = copy_task(tmp_path, "mini-mail")
    set_meta(root, "version = 1\n", f'version = 1\n{field} = "invalid"\n')
    with pytest.raises(TaskError):
        load_task(root)


def test_rebinding_the_persona_moves_every_whole_address_and_keeps_the_task_identity() -> None:
    task = load_task(FIXTURES / "mini-mail")
    rebound = rebind_persona(task, "px-1@product.test")
    assert rebound.seed.user.email == "px-1@product.test"
    messages = rebound.seed.services["mail"]["messages"]
    assert {tuple(m["to"]) for m in messages} == {("px-1@product.test",)}
    assert (rebound.hash, rebound.path, rebound.checks) == (task.hash, task.path, task.checks)  # no check names Alex
    assert rebind_persona(task, "ALEX@northwind.test") is task


def test_rebinding_keeps_the_task_files_under_a_directory_named_after_the_persona(tmp_path: Path) -> None:
    root = tmp_path / "alex@northwind.test" / "mini-mail"
    shutil.copytree(FIXTURES / "mini-mail", root)
    task = load_task(root)
    assert rebind_persona(task, "px-1@product.test").path == root


def test_rebinding_replaces_only_the_whole_address_in_any_case() -> None:
    text = "Alex@Northwind.test, xalex@northwind.test, alex@northwind.testing, <alex@northwind.test>"
    assert rebind_value(text, "alex@northwind.test", "me@x.test") == (
        "me@x.test, xalex@northwind.test, alex@northwind.testing, <me@x.test>"
    )


def test_lint_reads_a_globbed_user_ask_check_as_grading_asking(tmp_path: Path) -> None:
    name = "restraint-chat-no-reask-given-details"  # its calls check names the tool as "user_ask*"
    root = tmp_path / name
    shutil.copytree(task_module.REPO_ROOT / "tasks" / "core" / name, root)
    checks = root / "tests" / "checks.yaml"
    checks.write_text(checks.read_text().replace("[mail.drafts, user.interactions]", "[mail.drafts]"))
    assert any("user_ask calls check grades asking" in p for p in lint(load_task(root)))
