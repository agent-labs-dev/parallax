"""Shared harness machinery: the subprocess turn loop and the registry."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Mapping
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from parallax.models import (
    CostSource,
    HarnessInfo,
    Trial,
    TurnInput,
    TurnOutput,
    TurnStatus,
)
from parallax.protocols import BoxHandle, Harness

STDERR_TAIL_CHARS = 4000
RAW_OUTPUT_CHARS = 2 * 1024 * 1024  # the newest raw harness output kept per trial for diagnosis
VERSION_TIMEOUT_S = 60.0
ENTRY_POINT_GROUP = "parallax.harnesses"

# Controls live in modules another slice owns; they resolve lazily, exactly like plugin entry points.
CONTROLS: dict[str, tuple[str, tuple[str, ...]]] = {
    "null-claim": ("parallax.harnesses.null:NullHarness", ("claim",)),
    "null-refuse": ("parallax.harnesses.null:NullHarness", ("refuse",)),
    "oracle": ("parallax.harnesses.oracle:OracleHarness", ()),
}


class HarnessError(RuntimeError):
    """A harness could not be loaded or started; the runner records the trial as a boot error."""


class UnknownHarness(HarnessError):
    pass


def config_hash(config: BaseModel) -> str:
    """Identity of a harness configuration for result fingerprints."""
    # Sets are sorted: their iteration order changes with PYTHONHASHSEED, which made the
    # hash (and so every resume key) differ between processes.
    canonical = json.dumps(config.model_dump(), sort_keys=True, separators=(",", ":"), default=_canonical_json)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _canonical_json(value: object) -> object:
    if isinstance(value, set | frozenset):
        return sorted(to_jsonable_python(v) for v in value)
    return to_jsonable_python(value)


# ---------------------------------------------------------------------------
# harness-native credentials (for harnesses the meter cannot route)
# ---------------------------------------------------------------------------

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
# The providers the meter serves: a harness on them is metered, so their keys never enter a box.
METERED_PROVIDER_ENV = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
})  # fmt: skip
PARALLAX_ENV_PREFIX = "PARALLAX_"  # the runner's own control plane (world/meter admin, upstream keys)


def credential_env_problems(info: HarnessInfo) -> list[str]:
    """Why `info.credential_env` may not be forwarded; empty when it may."""
    if not info.credential_env:
        return []
    problems: list[str] = []
    if info.cost_source is CostSource.METERED:
        problems.append("credential_env is only for unmetered harnesses; a metered one reaches its model via the meter")
    for name in info.credential_env:
        if not _ENV_NAME.match(name):
            problems.append(f"credential_env {name!r} is not an UPPER_SNAKE env var name")
        elif name.startswith(PARALLAX_ENV_PREFIX):
            problems.append(f"credential_env {name!r} is parallax control-plane configuration")
        elif name in METERED_PROVIDER_ENV:
            problems.append(f"credential_env {name!r} belongs to a provider the meter serves")
    return problems


def native_credentials(info: HarnessInfo, environ: Mapping[str, str]) -> dict[str, str]:
    """The declared host credentials to forward into the box. A harness that declares some but finds
    none set cannot authenticate, so that is a boot error rather than a run that fails every turn."""
    problems = credential_env_problems(info)
    if problems:
        raise HarnessError(f"{info.name}: " + "; ".join(problems))
    found = {name: environ[name] for name in info.credential_env if environ.get(name)}
    if info.credential_env and not found:
        raise HarnessError(f"{info.name}: set one of {', '.join(info.credential_env)} on the host to authenticate it")
    return found


class OutputTail:
    """The newest raw output of a harness process, bounded, for the trial's harness.log.

    Chunks are kept whole so an append costs the chunk, not the log: an ACP agent writes one line per
    streamed token, and re-copying a 2 MiB string per line would be quadratic."""

    def __init__(self, limit: int = RAW_OUTPUT_CHARS) -> None:
        self.limit = limit
        self.chunks: deque[str] = deque()
        self.size = 0

    def add(self, text: str) -> None:
        self.chunks.append(text)
        self.size += len(text)
        while len(self.chunks) > 1 and self.size - len(self.chunks[0]) >= self.limit:
            self.size -= len(self.chunks.popleft())

    def text(self) -> str:
        joined = "".join(self.chunks)
        if len(joined) <= self.limit:
            return joined
        # Start at a line boundary: a cut mid-line could leave the tail of a secret that redaction,
        # which matches whole values, would no longer recognise.
        tail = joined[-self.limit :]
        return tail[tail.find("\n") + 1 :]


def requested_model(trial: Trial) -> str:
    """The label usage is attributed to when a CLI does not say which model served it."""
    return trial.model if trial.model is not None else "unreported"


class SubprocessHarness(ABC):
    """One process per turn. A subclass supplies `write_config`, `argv` and `parse`.

    The base keeps the session id (reset on `new_session`), writes each prompt to
    `{home}/turns/NN.md`, waits for the process to exit and keeps the last stderr.
    """

    #: `preassigned` harnesses take a runner-minted `{session}`; others learn it from their output.
    preassigned_session: bool = False

    def __init__(self, info: HarnessInfo) -> None:
        self._info = info
        self._trial: Trial | None = None
        self._box: BoxHandle | None = None
        self.session_id: str | None = None
        self.in_session = False
        self.stderr_tail = ""
        self.output_tail = OutputTail()  # every turn's stdout and stderr; written to the trial's harness.log

    @property
    def info(self) -> HarnessInfo:
        return self._info

    @property
    def trial(self) -> Trial:
        if self._trial is None:
            raise HarnessError(f"{self.info.name}: turn() before start()")
        return self._trial

    @property
    def box(self) -> BoxHandle:
        if self._box is None:
            raise HarnessError(f"{self.info.name}: turn() before start()")
        return self._box

    @abstractmethod
    async def write_config(self, trial: Trial) -> None: ...

    @abstractmethod
    def argv(self, turn: TurnInput, *, continuing: bool, session_id: str | None, prompt_file: Path) -> list[str]: ...

    @abstractmethod
    def parse(self, stdout: str, stderr: str) -> TurnOutput: ...

    def env(self) -> dict[str, str]:
        return dict(self.trial.env)

    def stdin(self, turn: TurnInput) -> bytes | None:
        return turn.text.encode()

    async def start(self, trial: Trial, box: BoxHandle) -> None:
        self._trial, self._box = trial, box
        self.new_session()
        await self.write_config(trial)

    def new_session(self) -> None:
        self.session_id = str(uuid.uuid4()) if self.preassigned_session else None
        self.in_session = False

    async def turn(self, t: TurnInput) -> TurnOutput:
        if t.new_session and self.in_session:
            self.new_session()
        if self.in_session and self.session_id is None:
            return TurnOutput(
                status=TurnStatus.HARNESS_ERROR,
                answer="",
                session_id=None,
                error="previous turn reported no session id, so this turn cannot continue it",
            )
        prompt_file = self.trial.home / "turns" / f"{t.index:02d}.md"
        await self.box.write_file(prompt_file, t.text.encode())
        argv = self.argv(t, continuing=self.in_session, session_id=self.session_id, prompt_file=prompt_file)
        proc = await self.box.spawn(argv, env=self.env(), cwd=self.trial.workspace)
        # No deadline here: the runner's `--turn-timeout` cancels the wait, which kills the process tree.
        code, out, err = await proc.communicate(self.stdin(t), None)
        self.stderr_tail = err[-STDERR_TAIL_CHARS:]
        self.output_tail.add(f"=== turn {t.index} exit {code} ===\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n")
        self.in_session = True
        parsed = self.stamp(self.parse(out, err), t.index)
        self.session_id = parsed.session_id or self.session_id
        if code != 0 and parsed.status is TurnStatus.COMPLETED:
            last = err.strip().splitlines()[-1:] or ["no stderr"]
            return parsed.model_copy(update={"status": TurnStatus.HARNESS_ERROR, "error": f"exit {code}: {last[0]}"})
        return parsed

    def stamp(self, out: TurnOutput, index: int) -> TurnOutput:
        """Fill in what a pure parser cannot know: the turn index and the requested model."""
        steps = tuple(s.model_copy(update={"turn": index}) for s in out.steps)
        usage = tuple(u if u.model else u.model_copy(update={"model": requested_model(self.trial)}) for u in out.usage)
        return out.model_copy(update={"steps": steps, "usage": usage})

    async def stop(self) -> None:
        return None  # a turn's process never outlives its turn: a cancelled wait kills it

    async def version(self, box: BoxHandle) -> str:
        return await run_version(self.info, box)


async def run_version(info: HarnessInfo, box: BoxHandle) -> str:
    if not info.version_cmd:
        raise HarnessError(f"{info.name}: no version_cmd declared")
    proc = await box.spawn(info.version_cmd, env={}, cwd=box.workspace)
    code, out, err = await proc.communicate(None, VERSION_TIMEOUT_S)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if code != 0 or not lines:
        raise HarnessError(f"{info.name}: {' '.join(info.version_cmd)} exited {code}: {err.strip()[-300:]}")
    return lines[0]


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def load_harness(name: str) -> Harness:
    """Built-in manifest, then a `parallax.harnesses` entry point, then a control."""
    from parallax.harnesses.manifest import harness_from_manifest, manifest_path

    if manifest_path(name).is_file():
        return harness_from_manifest(name)
    matches = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if len(matches) > 1:
        raise HarnessError(
            f"multiple harness entry points for {name!r}: {', '.join(sorted(ep.value for ep in matches))}"
        )
    if matches:
        return instantiate(matches[0], ())
    if name in CONTROLS:
        target, args = CONTROLS[name]
        return instantiate(EntryPoint(name=name, value=target, group=ENTRY_POINT_GROUP), args)
    raise UnknownHarness(f"unknown harness {name!r}; known: {', '.join(sorted(known_names()))}")


def instantiate(ep: EntryPoint, args: tuple[str, ...]) -> Harness:
    factory = ep.load()
    harness = factory(*args)
    if not callable(getattr(harness, "turn", None)) or not isinstance(harness.info, HarnessInfo):
        raise HarnessError(f"{ep.value} does not implement the Harness protocol")
    return harness


def known_names() -> set[str]:
    from parallax.harnesses.manifest import manifest_names

    return {*manifest_names(), *(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP)), *CONTROLS}


def available() -> dict[str, HarnessInfo | str]:
    """Every known harness mapped to its info, or to the reason it cannot run on this host."""
    from parallax.harnesses.manifest import ManifestError, load_manifest, manifest_path

    out: dict[str, HarnessInfo | str] = {}
    for name in sorted(known_names()):
        if manifest_path(name).is_file():
            try:
                spec = load_manifest(manifest_path(name))
            except ManifestError as exc:
                out[name] = f"invalid manifest: {exc}"
                continue
            binary = spec.argv_first[0]
            out[name] = spec.info if shutil.which(binary) else f"binary not on PATH: {binary}"
            continue
        try:
            out[name] = load_harness(name).info
        except Exception as exc:  # a probe: any failure to import or construct is the reason
            out[name] = f"{type(exc).__name__}: {exc}"
    return out
