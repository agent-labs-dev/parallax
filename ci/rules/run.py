"""Run Parallax's repository rules: the Python checks in checks/, one rule per file named by its id.

Each check exposes `check(files) -> [(path, line, message)]` and runs from the repository root over
every tracked or new file. A hit is suppressed by a whole-line `# rule-ignore: <id> - <reason>`
comment at most three lines above it, where the reason states the contract that makes the site
safe. A marker that suppresses nothing is an error. `--test` runs the checks against their
fixtures in check-tests/<id>/<case>/, whose `expected` lists the `path:line` each case reports.
"""

import argparse
import os
import re
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FIXTURES = "ci/rules/check-tests/"
MARKER = re.compile(r"\s*#\s*rule-ignore: ([a-z0-9-]+) - (.+)")
MARKED_SUFFIXES = (".py", ".yml", ".yaml", ".toml", ".sh")

type Hit = tuple[str, str, int, str]  # rule id, path, line, message
type Check = Callable[[list[str]], list[tuple[str, int, str]]]


def load_checks() -> dict[str, Check]:
    return {path.stem: runpy.run_path(str(path))["check"] for path in sorted((HERE / "checks").glob("*.py"))}


def corpus() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    paths = out.decode().split("\0")
    return sorted(p for p in paths if p and not p.startswith(FIXTURES) and (ROOT / p).is_file())


def run(files: list[str], checks: dict[str, Check]) -> list[Hit]:
    """Every unsuppressed hit, plus one per malformed or unused marker."""
    hits = [(rule_id, *hit) for rule_id, check in checks.items() for hit in check(files)]
    covered: set[Hit] = set()
    errors: list[Hit] = []
    for path in files:
        if not path.endswith(MARKED_SUFFIXES):
            continue
        text = Path(path).read_text(errors="replace")
        if "rule-ignore:" not in text:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if "rule-ignore:" not in line or not line.lstrip().startswith("#"):
                continue
            marker = MARKER.fullmatch(line.rstrip())
            if marker is None or marker[1] not in checks:
                errors.append(("rule-ignore", path, number, "write `# rule-ignore: <known id> - <the safe contract>`"))
                continue
            mine = {h for h in hits if h[0] == marker[1] and h[1] == path and number <= h[2] <= number + 3}
            covered |= mine
            if not mine:
                errors.append((marker[1], path, number, "this marker suppresses nothing; delete it"))
    return [h for h in hits if h not in covered] + errors


def test(checks: dict[str, Check]) -> list[Hit]:
    failures = []
    for rule_id in checks:
        cases = sorted((ROOT / FIXTURES / rule_id).iterdir())
        if not cases:
            failures.append((rule_id, "", 0, "has no fixtures"))
        for case in cases:
            head = case / "head"
            os.chdir(head)
            files = sorted(p.relative_to(head).as_posix() for p in head.rglob("*") if p.is_file())
            actual = {f"{h[1]}:{h[2]}" for h in run(files, {rule_id: checks[rule_id]})}
            expected = set((case / "expected").read_text().split())
            if actual != expected:
                failures.append(
                    (rule_id, "", 0, f"{case.name}: missing {expected - actual}, extra {actual - expected}")
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", action="store_true", help="run the checks against their fixtures")
    args = parser.parse_args()
    checks = load_checks()
    os.chdir(ROOT)
    hits = test(checks) if args.test else run(corpus(), checks)
    for rule_id, path, line, message in sorted(hits, key=lambda h: (h[1], h[2], h[0])):
        if "GITHUB_ACTIONS" in os.environ and path:  # an annotation on the line, shown in the PR diff
            print(f"::error file={path},line={line},title={rule_id}::{message}")
        else:
            print(f"{path}:{line}: {rule_id} {message}" if path else f"{rule_id}: {message}")
    print(f"{len(hits)} finding(s)" if hits else "ci/rules: clean")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
