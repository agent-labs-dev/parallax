"""The export scanner reports one planted violation per rule and nothing on clean text.

Planted strings are assembled at runtime so this file, which is itself exported
and scanned, carries none of them literally.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "scan_public.py"


def load_scanner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_public", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through sys.modules, so the module must be registered first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scanner = load_scanner()
Rule = scanner.Rule


def plant(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def write_denylist(where: Path, body: str) -> Path:
    path = where / "denylist.txt"
    path.write_text(body)
    return path


def rules_hit(root: Path, *, tree: bool = True, docker: bool = False) -> set[str]:
    denylist = scanner.resolve_denylist(write_denylist(root.parent, ""))
    hits = scanner.scan(root.resolve(), tree=tree, wheel=False, docker=docker, denylist=denylist)
    return {str(h.rule) for h in hits}


@pytest.mark.parametrize(
    ("rel", "text", "rule"),
    [
        ("staging/x/README.md", "x", Rule.STAGING_PATH),
        ("runs/20260101T000000Z/manifest.json", "{}", Rule.RUN_OUTPUT),
        ("docs/results.jsonl", "{}", Rule.RUN_OUTPUT),
        ("examples/upload.json", "{}", Rule.RUN_OUTPUT),
        ("docs/big.json", "[" + "0," * 200_000 + "0]", Rule.RUN_OUTPUT),
        ("docs/a.md", "ran it in " + "Fast" + "browse", Rule.PRIVATE_TERM),
        ("docs/results/fixture.json", "{}", Rule.PRIVATE_REPORT),
        (".envrc", "x", Rule.ENV_FILE),
        (".env.local", "x", Rule.ENV_FILE),
        ("parallax/tasks/core/t/.env", "x", Rule.ENV_FILE),
        ("docs/a.md", "see " + "trig" + "_06a7aea4 for context", Rule.PRIVATE_ID),
        ("docs/a.md", "thread " + "thrd" + "_0123abcdef", Rule.PRIVATE_ID),
        ("parallax/a.py", "load(" + "neb" + "ula_versions)", Rule.PRIVATE_TERM),
        ("parallax/a.py", "cost_in_" + "micro" + "cents = 1", Rule.PRIVATE_TERM),
        ("docs/a.md", "our gross " + "mar" + "gin is thin", Rule.PRIVATE_TERM),
        ("parallax/a.py", "from " + "src.models import User", Rule.PRIVATE_TERM),
        ("parallax/a.py", "import " + "src", Rule.PRIVATE_TERM),
        ("tasks/core/t/world/seed.yaml", "to: jane@" + "realcompany.com", Rule.TASK_EMAIL),
        ("parallax/tasks/core/t/world/seed.yaml", "to: jane@" + "realcompany.com", Rule.TASK_EMAIL),
        ("parallax/a.py", 'KEY = "' + "sk" + "-" + "a1B2" * 6 + '"', Rule.SECRET),
        ("parallax/a.py", 'TOKEN = "' + "ghp" + "_" + "Z9" * 12 + '"', Rule.SECRET),
        ("docs/a.md", "Authorization: " + "Bearer " + "q" * 24, Rule.SECRET),
    ],
)
def test_each_rule_reports_its_planted_violation(tmp_path: Path, rel: str, text: str, rule: str) -> None:
    root = tmp_path / "tree"
    plant(root, rel, text)
    assert rules_hit(root) == {rule}


def test_denylist_matches_literals_and_regexes_case_insensitively(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    plant(root, "tasks/core/t/instruction.md", "Forward it to ZORBLAX Holdings today.")
    plant(root, "docs/b.md", "tracked in widget#4512")
    denylist = write_denylist(tmp_path, "# comment\nzorblax holdings\nre:\\bwidget#\\d+\n")
    hits = scanner.scan(
        root.resolve(), tree=True, wheel=False, docker=False, denylist=scanner.resolve_denylist(denylist)
    )
    assert sorted((h.rule, h.path) for h in hits) == [
        (Rule.DENYLIST, "docs/b.md"),
        (Rule.DENYLIST, "tasks/core/t/instruction.md"),
    ]


def test_clean_text_near_every_rule_is_not_reported(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    plant(root, "parallax/report.css", "body { margin: 0; margin-top: 4px }")
    plant(root, "docs/methodology.md", "the margin of error shrinks; a task-runner and risk-free sk-short")
    plant(root, "tasks/core/t/world/seed.yaml", "a@corp.test b@x.example c@example.com d@y.invalid")
    plant(root, "parallax/a.py", "from parallax.srcmap import x\nws_name = 'news_feed'\n")
    assert rules_hit(root) == set()


def test_docker_context_admits_only_what_dockerignore_allows(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    plant(root, ".dockerignore", "staging/\n.env*\ntests/\n!tests/keep.txt\n")
    plant(root, "staging/private/cases.py", "x")
    plant(root, ".env", "x")
    plant(root, "tests/keep.txt", "import " + "src")
    plant(root, "tests/drop.txt", "import " + "src")
    admitted = {p.relative_to(root).as_posix() for p in scanner.docker_context_files(root)}
    assert admitted == {".dockerignore", "tests/keep.txt"}
    assert rules_hit(root, tree=False, docker=True) == {Rule.PRIVATE_TERM}


def test_cli_exits_nonzero_on_a_hit_and_zero_when_clean(tmp_path: Path) -> None:
    denylist = write_denylist(tmp_path, "")
    root = tmp_path / "tree"
    plant(root, "README.md", "clean")
    assert scanner.main([str(root), "--denylist", str(denylist)]) == 0
    plant(root, ".envrc", "x")
    assert scanner.main([str(root), "--denylist", str(denylist)]) == 1


def test_git_checkout_scans_tracked_and_untracked_files_but_not_ignored_ones(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    leak = "import " + "src"
    plant(root, ".gitignore", "docs/cache/\n")
    plant(root, "docs/cache/blob.txt", leak)
    plant(root, "staging/private/x.md", leak)
    plant(root, "docs/tracked.md", "clean")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "docs/tracked.md", "staging"], check=True)
    plant(root, "docs/untracked.md", leak)
    scanned = {p.relative_to(root).as_posix() for p in scanner.tree_files(root.resolve())}
    assert scanned == {".gitignore", "docs/tracked.md", "docs/untracked.md", "staging/private/x.md"}
    # A directory nested in the tree but ignored by it is walked, not vacuously empty.
    nested = {p.relative_to(root).as_posix() for p in scanner.tree_files((root / "docs" / "cache").resolve())}
    assert nested == {"docs/cache/blob.txt"}


def test_hit_output_masks_secrets_and_private_ids(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    denylist = write_denylist(tmp_path, "")
    root = tmp_path / "tree"
    key = "sk" + "-" + "A" + "x9Q" * 16 + "zz"
    private_id = "thrd" + "_0123abcdef"
    plant(root, "parallax/a.py", f'KEY = "{key}"\nTHREAD = "{private_id}"\n')
    assert scanner.main([str(root), "--denylist", str(denylist)]) == 1
    out = capsys.readouterr()
    printed = out.out + out.err
    assert key not in printed
    assert private_id not in printed
    assert f"sk-A…({len(key)})" in printed
    assert f"thrd…({len(private_id)})" in printed


def test_private_comparison_snapshots_are_excluded_from_real_export_and_build_context() -> None:
    root = SCRIPT.parent.parent
    for paths in (scanner.tree_files(root), scanner.docker_context_files(root)):
        assert scanner.DEFAULT_DENYLIST not in paths
        assert not any(p.relative_to(root).parts[:2] == ("docs", "results") for p in paths)


@pytest.mark.parametrize(
    ("rel", "local", "suffix", "extra", "expected"),
    [
        ("CODE_OF_CONDUCT.md", "support", "", "", {Rule.DENYLIST}),
        ("README.md", "support", "", "", {Rule.DENYLIST}),
        ("docs/CODE_OF_CONDUCT.md", "support", "", "", {Rule.DENYLIST}),
        ("CODE_OF_CONDUCT.md", "other", "", "", {Rule.DENYLIST}),
        ("CODE_OF_CONDUCT.md", "support", ".evil.test", "", {Rule.DENYLIST}),
        ("CODE_OF_CONDUCT.md", "support", "-internal", "", {Rule.DENYLIST}),
        ("CODE_OF_CONDUCT.md", "support", ".x", "", {Rule.DENYLIST}),
        ("CODE_OF_CONDUCT.md", "support", "\u00e9", "", {Rule.DENYLIST}),
        ("CODE_OF_CONDUCT.md", "support", "\u0301", "", {Rule.DENYLIST}),
        ("CODE_OF_CONDUCT.md", "support", "", " domain", {Rule.DENYLIST}),
        ("CODE_OF_CONDUCT.md", "support", "", " secret", {Rule.DENYLIST, Rule.SECRET}),
        ("tasks/core/t/instruction.md", "support", "", "", {Rule.DENYLIST, Rule.TASK_EMAIL}),
    ],
)
def test_denylisted_contact_domains_are_rejected_in_every_document(
    tmp_path: Path, rel: str, local: str, suffix: str, extra: str, expected: set[str]
) -> None:
    domain = "private" + "co.gg"
    address = f"{local}@{domain}{suffix}"
    text = f"Report to [{address}](mailto:{address})."
    if extra == " domain":
        text += f" Infrastructure: {domain}."
    elif extra == " secret":
        text += " Key: " + "sk" + "-" + "A" * 24
    root = tmp_path / "tree"
    plant(root, rel, text)
    denylist = scanner.resolve_denylist(write_denylist(tmp_path, domain))
    hits = scanner.scan(root.resolve(), tree=True, wheel=False, docker=False, denylist=denylist)
    assert {h.rule for h in hits} == expected


def test_public_export_keeps_offline_test_configuration() -> None:
    root = SCRIPT.parent.parent
    paths = {p.relative_to(root) for p in scanner.tree_files(root)}
    assert Path("tests/conftest.py") in paths


def test_hit_output_shows_nothing_of_a_denylisted_term(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    denylist = write_denylist(tmp_path, "re:zorblax \\w+\n")
    root = tmp_path / "tree"
    plant(root, "docs/a.md", "Forward it to Zorblax Holdings today.")
    assert scanner.main([str(root), "--denylist", str(denylist)]) == 1
    out = capsys.readouterr()
    printed = out.out + out.err
    assert "Zorb" not in printed
    assert "\u2026(16)" in printed
