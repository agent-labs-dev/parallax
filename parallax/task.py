"""Task loading, discovery, SELECT and lint.

A task directory is data; this module is the only place that turns it into a `Task`, so
every authoring mistake that can be caught without running anything surfaces here: load
errors are fatal (`TaskError`), style and consistency problems are `lint` findings.
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import json
import os
import re
import tomllib
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import ValidationError

from parallax import extensions, plugins
from parallax.extensions import RESERVED, validate_blocks
from parallax.match import parse_when
from parallax.models import (
    AnswerCheck,
    Area,
    CallsCheck,
    Capability,
    CheckSet,
    CommandCheck,
    FileCheck,
    Match,
    Matcher,
    Maturity,
    Model,
    MutationExpect,
    MutationSet,
    RunOrigin,
    Split,
    StateCheck,
    StepSpec,
    SuiteManifest,
    Task,
    TaskMeta,
    TaskRecord,
    TaskToml,
    Turn,
    WorldPatch,
    WorldSeed,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = Path(__file__).resolve().parent / "tasks"
if not TASKS_DIR.is_dir():
    TASKS_DIR = REPO_ROOT / "tasks"
MANIFEST = "suite.toml"  # every suite root holds one; see SuiteManifest
LITE = "lite"  # selection suites over core, listed in tasks/<name>/suite.toml
DEFAULT_MATURITIES = frozenset({Maturity.CANDIDATE, Maturity.REGRESSION, Maturity.ASPIRATIONAL})

_SEED_KEYS = frozenset({"now", "user", "web", "user_rules"})
USER_INTERACTIONS = "user.interactions"  # the user service's record of every question and its reply
_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
# Conformance canaries predate the area-prefix rule and use the short "conf-" prefix.
_AREA_PREFIX = {area: f"{area.value}-" for area in Area} | {Area.CONFORMANCE: "conf-"}
_TRAILING_COMMENT = re.compile(r"<!--(?P<body>(?:(?!-->).)*)-->\s*\Z", re.DOTALL)


class TaskError(Exception):
    """A task directory cannot be loaded; the message names the path and the problem."""


class SelectError(ValueError):
    """A SELECT expression is malformed or names something that matches no task."""


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def load_task(path: Path | str) -> Task:
    root = Path(path)
    try:
        return _load(root)
    except (OSError, yaml.YAMLError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise TaskError(f"{root}: {exc}") from exc


def _load(root: Path) -> Task:
    raw = tomllib.loads(_read(root / "task.toml"))
    blocks = raw.get("metadata")
    plugin_blocks: dict[str, object] = {}
    if isinstance(blocks, dict):
        # Only `[metadata.parallax]` is core; every other namespace belongs to a registered extension,
        # as does each sub-table of it that an extension claims.
        plugin_blocks = {k: v for k, v in blocks.items() if k != RESERVED}
        core = blocks.get(RESERVED)
        if isinstance(core, dict) and (claimed := extensions.core_tables() & core.keys()):
            plugin_blocks |= {k: core[k] for k in claimed}
            core = {k: v for k, v in core.items() if k not in claimed}
        raw = {**raw, "metadata": {RESERVED: core} if core is not None else {}}
    toml = TaskToml.model_validate(raw)
    if RESERVED not in toml.metadata:
        raise TaskError(f"{root}: task.toml has no [metadata.parallax] table")
    meta = toml.metadata[RESERVED]
    if meta.split is Split.HOLDOUT and root.resolve().is_relative_to(REPO_ROOT):
        # HOLDOUT tasks must never be published: they are only valid from a private root.
        raise TaskError(f"{root}: split=holdout is only accepted from a root outside {REPO_ROOT}")
    task = Task(
        meta=meta,
        turns=_turns(root, toml.steps, meta.canary),
        seed=load_seed(root / "world" / "seed.yaml"),
        checks=CheckSet.model_validate(_yaml(root / "tests" / "checks.yaml")),
        mutations=_mutations(root / "tests" / "mutations.yaml"),
        oracle=_oracle_kind(root),
        path=root,
        hash=task_hash(root),
        extensions=validate_blocks(plugin_blocks),
    )
    extensions.check(task)
    return task


def rebind_persona(task: Task, email: str) -> Task:
    """The task as run for an agent signed in as `email`, an account the harness cannot choose (a
    product's own login). Every mention of the persona's address, in the seed, the turns and their
    patches, the checks and the mutations, becomes `email`, so the world and its grading know the user
    by the one address the agent was given. `hash` stays the task's own."""
    if task.seed is None:  # a world-less task names no persona to rebind
        return task
    seeded = task.seed.user.email
    if email.casefold() == seeded.casefold():
        return task
    rebound = Task.model_validate(rebind_value(task.model_dump(mode="json"), seeded, email))
    # The task's files stay where they are, even under a directory named after the persona.
    return rebound.model_copy(update={"path": task.path, "hash": task.hash})


def rebind_value[V](value: V, old: str, new: str) -> V:
    """`value` with every whole-address occurrence of `old` (any case) replaced by `new`."""
    pattern = re.compile(rf"(?<![\w.+-]){re.escape(old)}(?![\w-])", re.IGNORECASE)

    def walk(v: object) -> object:
        match v:
            case str():
                return pattern.sub(lambda _: new, v)
            case dict():
                return {k: walk(x) for k, x in v.items()}
            case list():
                return [walk(x) for x in v]
            case tuple():
                return tuple(walk(x) for x in v)
            case _:
                return v

    return cast(V, walk(value))  # walk keeps every value's shape


def _read(path: Path) -> str:
    if not path.is_file():
        raise TaskError(f"missing {path}")
    return path.read_text(encoding="utf-8")


def _yaml(path: Path) -> object:
    return yaml.safe_load(_read(path))


def _turns(root: Path, steps: tuple[StepSpec, ...], canary: str) -> tuple[Turn, ...]:
    single = root / "instruction.md"
    if not steps:
        return (Turn(index=1, name="turn-01", text=strip_canary(_read(single), canary), new_session=False),)
    if single.exists():
        raise TaskError(f"{root}: has both instruction.md and [[steps]]; multi-step text lives in steps/<name>/")
    turns = []
    for i, step in enumerate(steps, start=1):
        text = _read(root / "steps" / step.name / "instruction.md")
        turns.append(
            Turn(
                index=i,
                name=step.name,
                text=strip_canary(text, canary) if i == 1 else text,
                new_session=step.session == "new",
                attachments=step.attachments,
                patch=load_patch(root, step.patch) if step.patch else None,
                origin=step.origin,
                approval=step.approval,
            )
        )
    return tuple(turns)


def strip_canary(text: str, canary: str) -> str:
    """Drop the trailing canary comment: it marks the file for contamination scans, not the agent's prompt."""
    m = _TRAILING_COMMENT.search(text)
    if m is None or canary not in m["body"]:
        return text
    return text[: m.start()].rstrip() + "\n"


def load_seed(path: Path) -> WorldSeed:
    """seed.yaml puts each service at the top level; fold them into `services`."""
    raw = _yaml(path)
    if not isinstance(raw, dict):
        raise TaskError(f"{path}: expected a mapping")
    if "services" in raw:
        raise TaskError(f"{path}: put each service at the top level, not under `services:`")
    fields = {k: v for k, v in raw.items() if k in _SEED_KEYS}
    services = {k: v for k, v in raw.items() if k not in _SEED_KEYS}
    return WorldSeed.model_validate({**fields, "services": services})


def load_patch(root: Path, name: str) -> WorldPatch:
    return WorldPatch.model_validate(_yaml(root / "world" / "patches" / f"{name}.yaml") or {})


def _mutations(path: Path) -> MutationSet:
    return MutationSet.model_validate(_yaml(path) or {}) if path.exists() else MutationSet()


def _oracle_kind(root: Path) -> Literal["yaml", "py"]:
    has_yaml = (root / "solution" / "oracle.yaml").is_file()
    has_py = (root / "solution" / "solve.py").is_file()
    if has_yaml == has_py:
        raise TaskError(f"{root}: needs exactly one of solution/oracle.yaml and solution/solve.py")
    return "yaml" if has_yaml else "py"


def task_hash(root: Path) -> str:
    """sha256 over (relative path, bytes) of every file, in sorted path order."""
    digest = hashlib.sha256()
    for rel, path in sorted(_task_files(root)):
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _task_files(root: Path) -> Iterator[tuple[str, Path]]:
    # Bytecode appears when a scripted oracle is imported; it is not part of the task.
    for path in root.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            yield path.relative_to(root).as_posix(), path


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def discover(roots: Iterable[Path | str]) -> list[Task]:
    """Every task (a dir holding task.toml) under the given roots, sorted by id; ids must be unique."""
    by_path: dict[Path, Task] = {}
    for root in map(Path, roots):
        if not root.is_dir():
            raise TaskError(f"task root {root} is not a directory")
        for toml in sorted(root.rglob("task.toml")):
            key = toml.parent.resolve()
            if key not in by_path:
                by_path[key] = load_task(toml.parent)
    tasks = sorted(by_path.values(), key=lambda t: t.id)
    for a, b in zip(tasks, tasks[1:], strict=False):
        if a.id == b.id:
            raise TaskError(f"duplicate task id {a.id!r}: {a.path} and {b.path}")
    return tasks


# ---------------------------------------------------------------------------
# task sources: tasks a plugin declares in code
# ---------------------------------------------------------------------------

SOURCES = "parallax.task_sources"


def record_hash(record: TaskRecord) -> str:
    """A task source record's identity: sha256 over its canonical JSON (keys sorted, sets in order), so equal content
    is one hash whatever order a source builds it in, and a change to any field is a new hash."""
    dump = record.model_dump(mode="json")
    dump["requires"] = sorted(dump["requires"])
    return hashlib.sha256(json.dumps(dump, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def task_from_record(source: str, record: TaskRecord) -> Task:
    """The `Task` a source's record declares: one turn of its prompt, no world, and the checks of the grading policy it
    names. Its path is a label (`<source>/<id>`), not a directory: nothing is read from it."""
    if not _ID.fullmatch(record.id):
        raise TaskError(f"task source {source}: id {record.id!r} must match {_ID.pattern}")
    policy = extensions.grading_policies().get(record.grading)
    if policy is None:
        known = ", ".join(sorted(extensions.grading_policies()))
        raise TaskError(
            f"task source {source}: task {record.id} names grading policy {record.grading!r}; known: {known}"
        )
    if policy.grade is None:
        raise TaskError(f"task source {source}: grading policy {policy.id} cannot grade a world-less task (no grade)")
    digest = record_hash(record)
    meta = TaskMeta(
        id=record.id, version=record.version, area=record.area, summary=record.summary, requires=record.requires,
        split=record.split, maturity=record.maturity, provenance=record.provenance, xfail=record.xfail,
        canary=str(uuid.UUID(hex=digest[:32], version=4)),
    )  # fmt: skip
    task = Task.model_construct(
        meta=meta, turns=(Turn(index=1, name="turn-01", text=record.prompt, new_session=False),), seed=None,
        checks=CheckSet.model_construct(checks=()), mutations=MutationSet(), oracle=None,
        path=Path(f"<{source}>") / record.id, hash=digest, extensions={}, source=source, grading=record.grading,
        data=record.data,
    )  # fmt: skip
    try:
        checks = CheckSet(checks=tuple(policy.checks(task)))
        return Task.model_validate(task.model_copy(update={"checks": checks}).model_dump())
    except ValidationError as exc:
        raise TaskError(f"task source {source}: task {record.id}: {exc}") from exc


def source_tasks(
    suites: Mapping[str, Sequence[Path]] | None = None,
    source_names: Iterable[str] | None = None,
) -> list[Task]:
    """Every task each `parallax.task_sources` entry point declares, sorted by id. An entry point is a callable
    returning `TaskRecord`s; its name selects its tasks like a suite's, so it may not shadow one."""
    suites = suite_roots() if suites is None else suites
    selected = None if source_names is None else frozenset(source_names)
    tasks: list[Task] = []
    for ep in plugins.entry_points_in(SOURCES):
        if selected is not None and ep.name not in selected:
            continue
        if ep.name in suites:
            raise TaskError(f"task source {ep.value!r} may not shadow suite {ep.name!r}")
        records: Callable[[], Iterable[TaskRecord]] = ep.load()
        tasks += [task_from_record(ep.name, record) for record in records()]
    return _unique(tasks)


def _unique(tasks: Iterable[Task]) -> list[Task]:
    ordered = sorted(tasks, key=lambda t: t.id)
    for a, b in zip(ordered, ordered[1:], strict=False):
        if a.id == b.id:
            raise TaskError(f"duplicate task id {a.id!r}: {a.path} and {b.path}")
    return ordered


def suite_roots() -> dict[str, tuple[Path, ...]]:
    """Built-in suites (each `tasks/<name>/` holding a suite.toml) and every
    `parallax.suites` entry point. Every suite's first root holds its manifest."""
    suites: dict[str, tuple[Path, ...]] = (
        {p.name: (p,) for p in sorted(TASKS_DIR.iterdir()) if (p / MANIFEST).is_file()} if TASKS_DIR.is_dir() else {}
    )
    builtin = frozenset(suites)
    for ep in plugins.entry_points_in("parallax.suites"):
        if ep.name in builtin:
            raise TaskError(f"plugin suite {ep.value!r} may not shadow built-in suite {ep.name!r}")
        roots_fn: Callable[[], Iterable[Path | str]] = ep.load()
        suites[ep.name] = tuple(Path(r) for r in roots_fn())
    return suites


def suite_manifest(roots: Sequence[Path]) -> SuiteManifest:
    """The manifest in a suite's first root."""
    if not roots:
        raise TaskError("a suite needs at least one root")
    return _manifest(roots[0].resolve())


@functools.cache
def _manifest(root: Path) -> SuiteManifest:
    path = root / MANIFEST
    if not path.is_file():
        raise TaskError(f"suite root {root} has no {MANIFEST}")
    try:
        return SuiteManifest.model_validate(tomllib.loads(path.read_text()))
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise TaskError(f"{path}: {exc}") from exc


def members(name: str) -> frozenset[str]:
    """The task ids a built-in selection suite lists, without discovering any task."""
    root = (TASKS_DIR / name).resolve()
    return frozenset(_manifest(root).tasks) if (root / MANIFEST).is_file() else frozenset()


def lint_suites(tasks: Sequence[Task], suites: Mapping[str, Sequence[Path]]) -> list[str]:
    """Manifest problems: a selection suite must draw on a directory suite and list only its tasks."""
    problems: list[str] = []
    for name, roots in suites.items():
        manifest = suite_manifest(roots)
        if manifest.within is None:
            continue
        source = suites.get(manifest.within)
        if source is None or suite_manifest(source).within is not None:
            problems.append(f"suite {name}: within={manifest.within!r} is not a directory suite")
            continue
        present = {t.id for t in _in_roots(tasks, source)}
        problems += [
            f"suite {name}: {i!r} is not a task in {manifest.within}" for i in manifest.tasks if i not in present
        ]
    return problems


def resolve(
    terms: Sequence[str] | str,
    extra_roots: Sequence[Path] = (),
    suites: Mapping[str, Sequence[Path]] | None = None,
) -> list[Task]:
    """Discover every root the terms can reach (suites, --tasks dirs, path terms) and select."""
    suites = suite_roots() if suites is None else suites
    path_terms = [Path(t) for t in parse_select(terms).names if _is_path_term(t, suites)]
    roots = [*(r for rs in suites.values() for r in rs), *extra_roots, *(p for p in path_terms if p.is_dir())]
    names = parse_select(terms).names
    source_names = frozenset(ep.name for ep in plugins.entry_points_in(SOURCES) if ep.name in names)
    return select(_unique([*discover(roots), *source_tasks(suites, source_names)]), terms, suites)


# ---------------------------------------------------------------------------
# SELECT
# ---------------------------------------------------------------------------


class Selection(Model):
    names: tuple[str, ...] = ()  # suites, ids/globs, task paths: unioned
    areas: frozenset[Area] | None = None  # filters: intersected
    maturities: frozenset[Maturity] | None = None
    splits: frozenset[Split] | None = None
    requires_within: frozenset[Capability] | None = None


def parse_select(terms: Sequence[str] | str) -> Selection:
    words = terms.split() if isinstance(terms, str) else [w for t in terms for w in t.split()]
    names: list[str] = []
    filters: dict[str, frozenset[str]] = {}
    for word in words:
        key, op, value = _split_filter(word)
        if not op:
            names.append(word)
            continue
        if key in filters:
            raise SelectError(f"filter {key!r} given twice")
        filters[key] = frozenset(v for v in value.split(",") if v)
    try:
        return Selection(
            names=tuple(names),
            areas=_enum_set(Area, filters.get("area")),
            maturities=_enum_set(Maturity, filters.get("maturity")),
            splits=_enum_set(Split, filters.get("split")),
            requires_within=_enum_set(Capability, filters.get("requires")),
        )
    except ValueError as exc:
        raise SelectError(str(exc)) from exc


_FILTER_OPS = {"area": "=", "maturity": "=", "split": "=", "requires": "<="}


def _split_filter(word: str) -> tuple[str, str, str]:
    m = re.fullmatch(r"(?P<key>[a-z]+)(?P<op><=|=)(?P<value>.*)", word)
    if m is None:
        return word, "", ""
    key, op = m["key"], m["op"]
    if _FILTER_OPS.get(key) != op:
        raise SelectError(f"unknown filter {word!r}; use area=, maturity=, split= or requires<=")
    return key, op, m["value"]


def _enum_set[E: (Area, Maturity, Split, Capability)](
    enum: type[E], values: frozenset[str] | None
) -> frozenset[E] | None:
    if values is None:
        return None
    if not values:
        raise SelectError(f"empty {enum.__name__.lower()} filter")
    return frozenset(enum(v) for v in values)


def select(
    tasks: Sequence[Task],
    terms: Sequence[str] | str,
    suites: Mapping[str, Sequence[Path]] | None = None,
) -> list[Task]:
    """Union of the name terms (all tasks when there are none), intersected with the filters."""
    suites = suite_roots() if suites is None else suites
    sel = parse_select(terms)
    chosen: dict[Path, Task] = {}
    for name in sel.names:
        hits = _name_matches(name, tasks, suites)
        if not hits:
            raise SelectError(f"SELECT term {name!r} matches no task")
        chosen.update((t.path.resolve(), t) for t in hits)
    pool = list(chosen.values()) if sel.names else list(tasks)
    if sel.maturities is None:
        # A draft lives outside every suite (review/drafts), so one reached through a --tasks or path
        # root is selectable by id or glob; a draft inside a suite root stays hidden.
        curated = [r for rs in suites.values() for r in rs]
        pool = [
            t
            for t in pool
            if t.meta.maturity in DEFAULT_MATURITIES
            or (t.meta.maturity is Maturity.DRAFT and not _in_roots([t], curated))
        ]
    else:
        pool = [t for t in pool if t.meta.maturity in sel.maturities]
    kept = [t for t in pool if _passes(t, sel)]
    return sorted(kept, key=lambda t: t.id)


def _passes(task: Task, sel: Selection) -> bool:
    return (
        (sel.areas is None or task.meta.area in sel.areas)
        and (sel.splits is None or task.meta.split in sel.splits)
        and (sel.requires_within is None or task.meta.requires <= sel.requires_within)
    )


def _name_matches(name: str, tasks: Sequence[Task], suites: Mapping[str, Sequence[Path]]) -> list[Task]:
    if name in suites:
        manifest = suite_manifest(suites[name])
        if manifest.within is None:
            return _in_roots(tasks, suites[name])
        ids = frozenset(manifest.tasks)
        available = _in_roots(tasks, suites.get(manifest.within, ()))
        missing = ids - {t.id for t in available}
        if missing:
            raise SelectError(f"suite {name}: tasks missing from {manifest.within}: {', '.join(sorted(missing))}")
        return [t for t in available if t.id in ids]
    if sourced := [t for t in tasks if t.source == name]:
        return sourced
    if _is_path_term(name, suites):
        return _in_roots(tasks, (Path(name),))
    return [t for t in tasks if fnmatch.fnmatchcase(t.id, name)]


def _in_roots(tasks: Sequence[Task], roots: Sequence[Path]) -> list[Task]:
    resolved = [r.resolve() for r in roots]
    return [t for t in tasks if any(t.path.resolve().is_relative_to(r) for r in resolved)]


def _is_path_term(term: str, suites: Mapping[str, Sequence[Path]]) -> bool:
    if term in suites:
        return False
    return os.sep in term or "/" in term or term.startswith(".") or Path(term).is_dir()


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?P<domain>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_SECRETS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[osu]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bBearer [A-Za-z0-9._~+/-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
)
_FICTIONAL_TLDS = (".test", ".example")


def lint(task: Task) -> list[str]:
    """Authoring problems that loading cannot catch; an empty list means clean."""
    if task.source is not None:
        return list(_lint_source(task))
    return [
        *_lint_identity(task),
        *_lint_text(task),
        *_lint_canary(task),
        *_lint_requires(task),
        *_lint_services(task),
        *_lint_checks(task),
        *_lint_matchers(task),
        *_lint_mutations(task),
        *_lint_files(task),
    ]


def _lint_source(task: Task) -> Iterator[str]:
    """A source's task has no directory, world or oracle: its id, text and requirements are what can be wrong."""
    if not task.id.startswith(_AREA_PREFIX[task.meta.area]):
        yield f"id {task.id!r} must start with {_AREA_PREFIX[task.meta.area]!r} for area {task.meta.area.value}"
    texts = {"prompt": "\n".join(t.text for t in task.turns), "data": json.dumps(task.data, ensure_ascii=False)}
    for where, text in texts.items():
        for pattern in _SECRETS:
            if pattern.search(text):
                yield f"{where}: looks like a secret ({pattern.pattern})"


def _lint_identity(task: Task) -> Iterator[str]:
    meta = task.meta
    if not _ID.fullmatch(meta.id):
        yield f"id {meta.id!r} must match {_ID.pattern}"
    if task.path.name != meta.id:
        yield f"id {meta.id!r} must equal the directory name {task.path.name!r}"
    # The area prefix is the public corpus naming convention; fixture and plugin roots only need the regex.
    if task.path.resolve().is_relative_to(TASKS_DIR) and not meta.id.startswith(_AREA_PREFIX[meta.area]):
        yield f"id {meta.id!r} must start with {_AREA_PREFIX[meta.area]!r} (area {meta.area.value})"
    if meta.maturity is Maturity.DRAFT and _in_roots([task], (TASKS_DIR,)):
        yield "maturity=draft belongs under review/drafts/, never in a task root"
    if meta.xfail is not None and not meta.xfail.issue.startswith("https://"):
        yield "xfail.issue must be a public https:// issue URL"
    if (task.path / "extensions").exists() and task.path.resolve().is_relative_to(TASKS_DIR / "core"):
        yield "Core tasks may not carry extensions/"


def _text_files(task: Task) -> Iterator[tuple[str, str]]:
    for rel, path in _task_files(task.path):
        try:
            yield rel, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary fixtures cannot hold a readable address or token


def _lint_text(task: Task) -> Iterator[str]:
    synthetic = not extensions.real_accounts(task)
    for rel, text in _text_files(task):
        for m in _EMAIL.finditer(text if synthetic else ""):
            if not m["domain"].lower().endswith(_FICTIONAL_TLDS):
                yield f"{rel}: email {m[0]!r} must use a .test or .example domain"
        for pattern in _SECRETS:
            if pattern.search(text):
                yield f"{rel}: looks like a secret ({pattern.pattern})"


def _lint_canary(task: Task) -> Iterator[str]:
    canary = task.meta.canary
    try:
        if uuid.UUID(canary).version != 4:
            yield f"canary {canary!r} must be a uuid4"
    except ValueError:
        yield f"canary {canary!r} is not a uuid"
    first = (
        "instruction.md" if (task.path / "instruction.md").exists() else f"steps/{task.turns[0].name}/instruction.md"
    )
    m = _TRAILING_COMMENT.search((task.path / first).read_text(encoding="utf-8"))
    if m is None or canary not in m["body"]:
        yield f"{first} must end with an HTML comment containing the canary {canary}"


def _lint_requires(task: Task) -> Iterator[str]:
    requires = task.meta.requires
    kinds = {c.kind for c in task.checks.checks}
    uses_workspace = kinds & {"file", "command"} or any(t.attachments for t in task.turns)
    if uses_workspace and Capability.WORKSPACE not in requires:
        yield "file/command checks or attachments need requires to include workspace"
    if any(not t.new_session for t in task.turns[1:]) and Capability.MULTI_TURN not in requires:
        yield "a continued step needs requires to include multi_turn"
    if Capability.TERMINAL in requires and Capability.WORKSPACE not in requires:
        yield "a terminal task reads its evidence from /workspace/.parallax-terminal, so it needs workspace"
    if any(t.origin is RunOrigin.JOB for t in task.turns) and Capability.UNATTENDED not in requires:
        yield "a job step needs requires to include unattended"
    for turn in task.turns:
        if turn.approval is not None and turn.origin is not RunOrigin.JOB:
            yield f'step {turn.name}: approval answers a job step\'s card; set origin = "job"'
        if turn.origin is RunOrigin.JOB and (turn.attachments or not turn.new_session and turn.index > 1):
            yield f'step {turn.name}: a job step starts its own run, so it takes no attachments and session = "new"'
    desktop = task.meta.desktop
    if (Capability.DESKTOP in requires) != (desktop is not None):
        yield "requires desktop and metadata.parallax.desktop must be declared together"
    if desktop is not None:
        if Capability.WORKSPACE not in requires:
            yield "desktop apps need requires to include workspace"
        seed = (task.path / "environment" / "files").resolve()
        for app in desktop.apps:
            path = Path(app.path)
            target = (seed / path).resolve()
            if path.is_absolute() or ".." in path.parts or not target.is_relative_to(seed):
                yield f"desktop app path must stay under environment/files: {app.path}"
            elif path.suffix != ".py" or not target.is_file():
                yield f"desktop app must be a seeded Python file: {app.path}"


def mounted_services(task: Task) -> frozenset[str]:
    """The services a task's world mounts: the defaults for an MCP task, plus its own."""
    from parallax.world.services import DEFAULT_SERVICES  # lazy: services are a sibling slice

    defaults = DEFAULT_SERVICES if Capability.MCP in task.meta.requires and extensions.default_services(task) else ()
    return frozenset(defaults) | frozenset(task.meta.services)


def _lint_services(task: Task) -> Iterator[str]:
    mounted = mounted_services(task)
    for svc in task.world.services:
        if svc not in mounted:
            yield f"seed has service {svc!r} which is not mounted (add it to services)"
    if task.world.web and "web" not in mounted:
        yield "seed has web pages but the web service is not mounted"
    for fault in task.meta.faults:
        if fault.tool.split("_", 1)[0] not in mounted:
            yield f"fault on {fault.tool!r}: service not mounted"
    # Asking the user writes a row to user.interactions. A task that grades asking with a `user_ask` calls
    # check exempts that table from its collateral check, so one question fails once.
    grades_asking = any(isinstance(c, CallsCheck) and fnmatch.fnmatch("user_ask", c.tool) for c in task.checks.checks)
    for check in task.checks.checks:
        if isinstance(check, StateCheck) and check.table != "*" and check.table.split(".", 1)[0] not in mounted:
            yield f"check {check.id}: table {check.table!r} belongs to an unmounted service"
        if (
            grades_asking
            and isinstance(check, StateCheck)
            and check.table == "*"
            and "user" in mounted
            and USER_INTERACTIONS not in check.except_tables
        ):
            yield f"check {check.id}: a user_ask calls check grades asking, so list {USER_INTERACTIONS} in except_tables"


def _lint_checks(task: Task) -> Iterator[str]:
    n_turns = len(task.turns) + len(task.meta.fires)
    for check in task.checks.checks:
        match check:
            case StateCheck(table="*", unchanged=False):
                yield f"check {check.id}: table='*' needs unchanged=true"
            case StateCheck(table=table, except_tables=except_tables) if except_tables and table != "*":
                yield f"check {check.id}: except_tables only applies with table='*'"
            case AnswerCheck() if not (check.must or check.must_not or check.in_order or check.known_urls_only):
                yield f"check {check.id}: answer check asserts nothing"
            case AnswerCheck(turn=turn) if turn != -1 and not 1 <= turn <= n_turns:
                yield f"check {check.id}: turn {turn} outside 1..{n_turns}"
            case FileCheck(path=path) if path.startswith("/") or ".." in Path(path).parts:
                yield f"check {check.id}: path must be relative to /workspace without '..'"
            case _:
                pass


def _check_matchers(task: Task) -> Iterator[tuple[str, Matcher, bool]]:
    """(where, matcher, is_text) for every authored matcher; text matchers read free text."""
    for c in task.checks.checks:
        for key, m in _field_matchers(c):
            yield f"check {c.id} {key}", m, False
        for m in _text_matchers(c):
            yield f"check {c.id}", m, True
    for fire in task.meta.fires:
        for key, m in fire.job.items():
            yield f"fire {fire.name} {key}", m, False
    for rule in task.world.user_rules:
        for key, m in rule.when.items():
            yield f"user_rule {key}", m, False


def _field_matchers(check: object) -> Iterator[tuple[str, Matcher]]:
    match check:
        case StateCheck(where=where, fields=fields):
            yield from where.items()
            yield from fields.items()
        case CallsCheck(where=where):
            yield from where.items()
        case _:
            return


def _text_matchers(check: object) -> tuple[Matcher, ...]:
    match check:
        case AnswerCheck(must=must, must_not=must_not):
            return (*must, *must_not)
        case FileCheck(contains=contains):
            return contains
        case CommandCheck(stdout=stdout):
            return stdout
        case _:
            return ()


def _lint_matchers(task: Task) -> Iterator[str]:
    tz, now = task.world.user.timezone, task.world.now
    for where, m, is_text in _check_matchers(task):
        if is_text and (m is None or isinstance(m, bool)):
            yield f"{where}: a text matcher must be a string, number or mapping, not {m!r}"
        if not isinstance(m, Match):
            continue
        if m.regex is not None:
            try:
                re.compile(m.regex)
            except re.error as exc:
                yield f"{where}: bad regex {m.regex!r}: {exc}"
        for bound in (m.gte, m.lte):
            if isinstance(bound, str) and parse_when(bound, tz, now) is None:
                yield f"{where}: unparseable datetime bound {bound!r}"


def _lint_mutations(task: Task) -> Iterator[str]:
    gradable = {c.id for c in task.checks.checks}
    seen: set[str] = set()
    for mut in task.mutations.mutations:
        if mut.id in seen:
            yield f"mutation {mut.id}: duplicate id"
        seen.add(mut.id)
        if mut.expect is MutationExpect.FAIL and not mut.must_fail:
            yield f"mutation {mut.id}: must_fail is empty"
        if mut.expect is MutationExpect.PASS and mut.must_fail:
            yield f"mutation {mut.id}: expect=pass is graded by every gate, so must_fail must be empty"
        if mut.expect is MutationExpect.PASS and mut.collateral:
            yield f"mutation {mut.id}: expect=pass cannot be the collateral mutant, which must be killed"
        for check_id in mut.must_fail:
            if check_id not in gradable:
                yield f"mutation {mut.id}: must_fail names {check_id!r}, not a check"


def _lint_files(task: Task) -> Iterator[str]:
    files = task.path / "environment" / "files"
    for turn in task.turns:
        for att in turn.attachments:
            if not (files / att.path).is_file():
                yield f"{turn.name}: attachment {att.path!r} not found under environment/files"
    for fire in task.meta.fires:
        if fire.patch is None:
            continue
        try:
            load_patch(task.path, fire.patch)
        except (TaskError, ValidationError, yaml.YAMLError) as exc:
            yield f"fire {fire.name}: patch {fire.patch!r}: {exc}"
