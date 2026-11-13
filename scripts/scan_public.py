"""Refuse to publish a tree that carries private material.

Usage: scan_public.py ROOT [--wheel] [--docker-context] [--no-tree] [--files PATH...] [--denylist PATH]

Exits 1 when any rule hits, printing one line per hit. The plain tree scan is the
publication gate; `--wheel` and `--docker-context` scan what a built wheel and a docker
build of ROOT would actually ship. Both must exclude private staging material.
`--no-tree` skips the source-tree scan; `--files` scans only the named files (the pre-commit hook).
Run output never belongs in the repository: Langfuse holds every result.

Several patterns below are written so they cannot match their own source text
(`n[e]bula`): this file is itself published and scanned.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

DEFAULT_DENYLIST = Path(__file__).resolve().with_name("denylist.txt")


class Rule(StrEnum):
    STAGING_PATH = "staging-path"
    PRIVATE_REPORT = "private-report"
    ENV_FILE = "env-file"
    PRIVATE_ID = "private-id"
    PRIVATE_TERM = "private-term"
    TASK_EMAIL = "task-email"
    SECRET = "secret"
    DENYLIST = "denylist"
    RUN_OUTPUT = "run-output"


class Source(StrEnum):
    TREE = "tree"
    WHEEL = "wheel"
    DOCKER = "docker-context"


@dataclass(frozen=True)
class Hit:
    rule: Rule
    source: Source
    path: str
    line: int | None
    excerpt: str

    def render(self) -> str:
        where = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{self.rule:<13} {self.source}:{where}  {self.excerpt}"


CONTENT_RULES: tuple[tuple[Rule, re.Pattern[str]], ...] = (
    (Rule.PRIVATE_ID, re.compile(r"\b(?:thrd|trig|usr|ws|chan)_[0-9a-f]{6,}")),
    (Rule.PRIVATE_TERM, re.compile(r"n[e]bula|ultra[f]ast|fast[b]rowse", re.IGNORECASE)),
    (Rule.PRIVATE_TERM, re.compile(r"micro[c]ents", re.IGNORECASE)),
    # Commercial markup in the business sense, not the CSS property (`margin:` / `margin-top`) or a statistical margin of error.
    (Rule.PRIVATE_TERM, re.compile(r"\bmargins?\b(?!\s*:|-|\s+of\s+error)", re.IGNORECASE)),
    (Rule.PRIVATE_TERM, re.compile(r"\bfrom\s+src\.")),
    (Rule.PRIVATE_TERM, re.compile(r"\bimport\s+src\b")),
    # `sk-` alone is in every "task-"/"risk-"; a key is a token start followed by a long body.
    (Rule.SECRET, re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")),
    (Rule.SECRET, re.compile(r"\bghp_[A-Za-z0-9]{20,}")),
    (Rule.SECRET, re.compile(r"\bBearer [A-Za-z0-9]{20,}")),
)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
# RFC 2606 / 6761 reserved names: the only domains a task may address.
RESERVED_TLDS = frozenset({"test", "example", "invalid", "localhost"})
RESERVED_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
# Hit lines land in CI logs, so a real credential or identifier printed in full would leak through the gate itself.
MASKED_RULES = frozenset({Rule.SECRET, Rule.PRIVATE_ID})
MASK_PREFIX = 4
# What `parallax run` writes. Only a test fixture may carry these names.
RUN_FILES = frozenset({"results.jsonl", "upload.json", "world_evidence.json", "trajectory.json", "provenance.json"})
LARGE_JSON_BYTES = 256 * 1024


def load_denylist(path: Path) -> tuple[re.Pattern[str], ...]:
    patterns: list[re.Pattern[str]] = []
    for raw in path.read_text().splitlines():
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        body = entry.removeprefix("re:") if entry.startswith("re:") else re.escape(entry)
        patterns.append(re.compile(body, re.IGNORECASE))
    return tuple(patterns)


def email_allowed(domain: str) -> bool:
    lowered = domain.lower()
    return lowered.rsplit(".", 1)[-1] in RESERVED_TLDS or any(
        lowered == d or lowered.endswith("." + d) for d in RESERVED_DOMAINS
    )


def is_fixture(rel: Path) -> bool:
    return rel.parts[:1] == ("tests",) and "fixtures" in rel.parts


def path_hits(rel: Path, source: Source) -> Iterator[Hit]:
    parts = rel.parts[1:] if rel.parts[:2] == ("parallax", "tasks") else rel.parts
    if parts[0] in ("staging", "suites"):
        yield Hit(Rule.STAGING_PATH, source, rel.as_posix(), None, "staged material must never ship")
    if not is_fixture(rel) and ("runs" in parts or rel.name in RUN_FILES or rel.suffix == ".jsonl"):
        yield Hit(Rule.RUN_OUTPUT, source, rel.as_posix(), None, "run output lives in Langfuse, not the repository")
    if parts[:2] == ("docs", "results"):
        yield Hit(Rule.PRIVATE_REPORT, source, rel.as_posix(), None, "private comparison snapshot must never ship")
    # A task's seeded workspace may deliberately ship a synthetic .env (the safety
    # tasks test that agents keep its values out of posts); anywhere else it is a leak.
    task_fixture = len(parts) > 4 and parts[0] == "tasks" and parts[3:5] == ("environment", "files")
    if not task_fixture and any(p == ".envrc" or p.startswith(".env") for p in parts):
        yield Hit(Rule.ENV_FILE, source, rel.as_posix(), None, "environment file")


def mask(value: str) -> str:
    return f"{value[:MASK_PREFIX]}\u2026({len(value)})"


def excerpt_for(rule: Rule, matched: str) -> str:
    # A deny-list term is the private name itself, and CI logs are public: none of it is shown, and the path and
    # line say where to look.
    if rule is Rule.DENYLIST:
        return f"\u2026({len(matched)})"
    return mask(matched) if rule in MASKED_RULES else matched[:80]


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def content_hits(rel: Path, text: str, source: Source, denylist: tuple[re.Pattern[str], ...]) -> Iterator[Hit]:
    rules = [*CONTENT_RULES, *((Rule.DENYLIST, p) for p in denylist)]
    for rule, pattern in rules:
        for m in pattern.finditer(text):
            yield Hit(rule, source, rel.as_posix(), line_of(text, m.start()), excerpt_for(rule, m.group(0)))
    if rel.parts[0] == "tasks" or rel.parts[:2] == ("parallax", "tasks"):
        for m in EMAIL.finditer(text):
            if not email_allowed(m.group(1)):
                yield Hit(Rule.TASK_EMAIL, source, rel.as_posix(), line_of(text, m.start()), m.group(0))


def scan_files(root: Path, files: Iterable[Path], source: Source, denylist: tuple[re.Pattern[str], ...]) -> list[Hit]:
    hits: list[Hit] = []
    for path in files:
        rel = path.relative_to(root)
        hits.extend(path_hits(rel, source))
        if rel.suffix == ".json" and not is_fixture(rel) and path.stat().st_size > LARGE_JSON_BYTES:
            hits.append(Hit(Rule.RUN_OUTPUT, source, rel.as_posix(), None, "large JSON outside test fixtures"))
        # Binary files are decoded lossily rather than skipped: a key inside a blob is still a leak.
        hits.extend(content_hits(rel, path.read_bytes().decode("utf-8", errors="replace"), source, denylist))
    return hits


def walk_files(root: Path) -> list[Path]:
    # VCS metadata is not content; everything else under ROOT is.
    return sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts)


def is_git_toplevel(root: Path) -> bool:
    # Only the top of a work tree: a directory nested in someone else's tree (an
    # unpacked archive under the ignored runs/) would list nothing and pass vacuously.
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return probe.returncode == 0 and Path(probe.stdout.strip()).resolve() == root.resolve()


def tree_files(root: Path) -> list[Path]:
    """What a push would publish when ROOT is the top of a git work tree, else every file under ROOT.

    Tracked plus untracked-not-ignored files, so ignored caches, .venv and run outputs are out of scope and a new
    untracked file is still scanned.
    """
    if not is_git_toplevel(root):
        return walk_files(root)
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        capture_output=True,
        check=True,
    )
    # A tracked file deleted in the working tree is listed but has nothing to scan.
    paths = (root / rel for rel in listed.stdout.decode().split("\0") if rel)
    return sorted(p for p in paths if p.is_file())


def translate_glob(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def dockerignore_rules(text: str) -> list[tuple[bool, re.Pattern[str]]]:
    rules: list[tuple[bool, re.Pattern[str]]] = []
    for raw in text.splitlines():
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        negated = entry.startswith("!")
        cleaned = entry.removeprefix("!").strip().strip("/")
        if cleaned:
            rules.append((negated, translate_glob(cleaned)))
    return rules


def docker_admits(rel: Path, rules: list[tuple[bool, re.Pattern[str]]]) -> bool:
    """Docker semantics: a pattern matching a file or any parent dir applies; the last match wins."""
    prefixes = ["/".join(rel.parts[: n + 1]) for n in range(len(rel.parts))]
    excluded = False
    for negated, pattern in rules:
        if any(pattern.fullmatch(p) for p in prefixes):
            excluded = not negated
    return not excluded


def docker_context_files(root: Path) -> list[Path]:
    ignore = root / ".dockerignore"
    if not ignore.is_file():
        print(f"WARNING: {ignore} missing; docker would send the whole tree", file=sys.stderr)
        return walk_files(root)
    rules = dockerignore_rules(ignore.read_text())
    # Docker sends the directory as it sits on disk, git-ignored files included.
    return [p for p in walk_files(root) if docker_admits(p.relative_to(root), rules)]


def scan_wheel(root: Path, denylist: tuple[re.Pattern[str], ...]) -> list[Hit]:
    with tempfile.TemporaryDirectory(prefix="px-wheel-") as tmp:
        out = Path(tmp) / "dist"
        build = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(out), str(root)], capture_output=True, text=True
        )
        if build.returncode != 0:
            raise SystemExit(f"uv build failed:\n{build.stderr}")
        wheels = sorted(out.glob("*.whl"))
        if len(wheels) != 1:
            raise SystemExit(f"expected one wheel in {out}, found {[w.name for w in wheels]}")
        extracted = Path(tmp) / "wheel"
        with zipfile.ZipFile(wheels[0]) as zf:
            zf.extractall(extracted)
        return scan_files(extracted, walk_files(extracted), Source.WHEEL, denylist)


def resolve_denylist(explicit: Path | None) -> tuple[re.Pattern[str], ...]:
    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(f"denylist not found: {explicit}")
        return load_denylist(explicit)
    if DEFAULT_DENYLIST.is_file():
        return load_denylist(DEFAULT_DENYLIST)
    # The deny list is private and never committed, so a public checkout legitimately lacks it.
    print(f"WARNING: {DEFAULT_DENYLIST} absent; the customer-name rule is NOT applied", file=sys.stderr)
    return ()


def scan(
    root: Path,
    *,
    tree: bool,
    wheel: bool,
    docker: bool,
    denylist: tuple[re.Pattern[str], ...],
    files: Iterable[Path] = (),
) -> list[Hit]:
    hits = scan_files(root, [p.resolve() for p in files if p.is_file()], Source.TREE, denylist)
    if tree:
        hits.extend(scan_files(root, tree_files(root), Source.TREE, denylist))
    if docker:
        hits.extend(scan_files(root, docker_context_files(root), Source.DOCKER, denylist))
    if wheel:
        hits.extend(scan_wheel(root, denylist))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a tree for material that must never be published.")
    parser.add_argument("root", type=Path)
    parser.add_argument("--wheel", action="store_true", help="build a wheel from ROOT with `uv build` and scan it")
    parser.add_argument("--docker-context", action="store_true", help="scan the files .dockerignore admits")
    parser.add_argument("--no-tree", action="store_true", help="skip the plain tree scan")
    parser.add_argument("--files", type=Path, nargs="*", default=[], help="scan only these files")
    parser.add_argument("--denylist", type=Path, default=None, help=f"default: {DEFAULT_DENYLIST}")
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    hits = scan(
        root,
        tree=not (args.no_tree or args.files),
        wheel=args.wheel,
        docker=args.docker_context,
        denylist=resolve_denylist(args.denylist),
        files=args.files,
    )
    for hit in hits:
        print(hit.render())
    print(f"{len(hits)} hit(s) in {root}", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
