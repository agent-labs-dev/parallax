"""The local gate selects unit suites for script edits and propagates their failures."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UNIT_TESTS = "run pytest -q tests/core tests/world tests/harnesses"


@pytest.mark.parametrize("script", ["scripts/scan_public.py", "scripts/gate", "scripts/helper.sh"])
def test_changed_script_runs_all_unit_tests(tmp_path: Path, script: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/gate", repo / "scripts/gate")
    for name in ("scripts/scan_public.py", "scripts/helper.sh"):
        (repo / name).write_text("# initial script\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "."],
        ["-c", "user.name=Gate Test", "-c", "user.email=gate@example.test", "commit", "-qm", "baseline"],
        ["update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    with (repo / script).open("a") as stream:
        stream.write("# changed script\n")

    tools = tmp_path / "bin"
    tools.mkdir()
    calls = tmp_path / "calls"
    uv = tools / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$GATE_CALL_LOG"\n'
        'if [[ "$1 $2" == "run pytest" ]]; then exit 42; fi\n'
        "exit 0\n"
    )
    uv.chmod(0o755)
    result = subprocess.run(
        ["bash", "scripts/gate", "--changed"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "GATE_CALL_LOG": str(calls)},
        capture_output=True,
        text=True,
    )
    commands = calls.read_text().splitlines()
    assert UNIT_TESTS in commands, f"{script} edit skipped unit tests: {commands}"
    assert result.returncode == 42, result.stdout + result.stderr
