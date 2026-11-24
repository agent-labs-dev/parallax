"""Declarative harnesses: a TOML `ManifestSpec` run as a CLI, one process per turn.

Placeholders are substituted into argv elements, env values and config-file strings (keys too, since
some configs key maps by model id). Substitution is typed and never passes through a shell: a
`{prompt}` element becomes exactly one argv element holding the prompt verbatim.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, assert_never

import yaml
from pydantic import JsonValue, ValidationError

from parallax.harnesses.base import STDERR_TAIL_CHARS, SubprocessHarness, config_hash, credential_env_problems
from parallax.harnesses.parsers import PARSERS
from parallax.models import (
    Capability,
    ConfigFile,
    JsonObject,
    ManifestSpec,
    Trial,
    TurnInput,
    TurnOutput,
    TurnStatus,
)
from parallax.protocols import BoxHandle, Harness

MANIFEST_DIR = Path(__file__).parent / "manifests"

PLACEHOLDERS = frozenset({
    "model", "model_provider", "model_name", "prompt", "prompt_file", "session", "workspace", "home",
    "world_url", "world_token", "world_token_env", "meter_openai", "meter_anthropic", "meter_key",
    "task_container",
})  # fmt: skip
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class ManifestError(ValueError):
    """A manifest that cannot be run as written: an authoring bug, never a harness failure."""


# ---------------------------------------------------------------------------
# loading and validation
# ---------------------------------------------------------------------------


def manifest_path(name: str) -> Path:
    return MANIFEST_DIR / f"{name}.toml"


def manifest_names() -> list[str]:
    return sorted(p.stem for p in MANIFEST_DIR.glob("*.toml"))


def load_manifest(path: Path) -> ManifestSpec:
    try:
        spec = ManifestSpec.model_validate(tomllib.loads(path.read_text()))
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ManifestError(f"{path.name}: {exc}") from exc
    problems = manifest_problems(spec, path.stem)
    if problems:
        raise ManifestError(f"{path.name}: " + "; ".join(problems))
    return spec


def manifest_problems(spec: ManifestSpec, stem: str) -> list[str]:
    problems: list[str] = []
    if spec.info.name != stem:
        problems.append(f"info.name {spec.info.name!r} must equal the file name {stem!r}")
    if not spec.info.version_cmd:
        problems.append("info.version_cmd is required")
    problems.extend(credential_env_problems(spec.info))
    for text in (
        *spec.argv_first, *spec.argv_next, *spec.tb2_argv_first, *spec.tb2_argv_next,
        spec.tb2_install, *spec.env.values(), *config_strings(spec.config),
    ):  # fmt: skip
        problems.extend(f"unknown placeholder {{{p}}}" for p in _PLACEHOLDER.findall(text) if p not in PLACEHOLDERS)
    problems.extend(cli_problems(spec))
    if bool(spec.tb2_argv_first) != (Capability.LIVE_CONTAINER in spec.info.capabilities):
        problems.append("live_container capability and tb2_argv_first must be declared together")
    if spec.tb2_argv_next and not spec.tb2_argv_first:
        problems.append("tb2_argv_next needs tb2_argv_first")
    if Capability.DESKTOP in spec.info.capabilities and not any(f.desktop_content for f in spec.config):
        problems.append("desktop is declared but no config file gives the harness the desktop (desktop_content)")
    return problems


def cli_problems(spec: ManifestSpec) -> list[str]:
    problems: list[str] = []
    if spec.parser is None:
        problems.append("cli manifests need a parser")
    if not spec.argv_next and Capability.MULTI_TURN in spec.info.capabilities:
        problems.append("multi_turn is declared but argv_next is empty")
    if spec.argv_next and not any("{session}" in a for a in spec.argv_next):
        problems.append("argv_next must reference {session}")
    if spec.session == "preassigned" and not any("{session}" in a for a in spec.argv_first):
        problems.append("preassigned sessions must pass {session} in argv_first")
    return problems


def config_strings(files: tuple[ConfigFile, ...]) -> list[str]:
    out: list[str] = []

    def walk(value: JsonValue) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, dict):
            for k, v in value.items():
                out.append(k)
                walk(v)

    for f in files:
        walk(f.content)
        for extra in (f.mcp_content, f.desktop_content):
            if extra is not None:
                walk(extra)
    return out


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def trial_values(trial: Trial) -> dict[str, str]:
    """Placeholder values known for the whole trial; absent inputs are simply not offered, so a
    template that needs one fails loudly in `render` instead of receiving an empty string."""
    values = {
        "workspace": str(trial.workspace),
        "home": str(trial.home),
    }
    if trial.mcp is not None:  # a world-less trial offers no world placeholders, so a template naming one fails
        values |= {"world_url": trial.mcp.url, "world_token_env": trial.mcp.token_env}
        if trial.mcp.token_env in trial.env:
            values["world_token"] = trial.env[trial.mcp.token_env]
    if trial.model is not None:
        provider, sep, name = trial.model.partition("/")
        values["model"] = trial.model
        if sep:
            values["model_provider"], values["model_name"] = provider, name
    if trial.task_container is not None:
        values["task_container"] = trial.task_container
    if trial.llm is not None:
        values["meter_openai"] = trial.llm.openai_base_url
        values["meter_anthropic"] = trial.llm.anthropic_base_url
        values["meter_key"] = trial.llm.api_key
    return values


def render(template: str, values: Mapping[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in PLACEHOLDERS:
            raise ManifestError(f"unknown placeholder {{{name}}}")
        if name not in values:
            raise ManifestError(f"placeholder {{{name}}} has no value for this trial")
        return values[name]

    return _PLACEHOLDER.sub(sub, template)


def render_json(value: JsonValue, values: Mapping[str, str]) -> JsonValue:
    if isinstance(value, str):
        return render(value, values)
    if isinstance(value, list):
        return [render_json(v, values) for v in value]
    if isinstance(value, dict):
        return {render(k, values): render_json(v, values) for k, v in value.items()}
    return value


def config_target(path: str, trial: Trial) -> Path:
    root, sep, rel = path.partition(":")
    if not sep or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ManifestError(f"config path {path!r} must be home:<rel> or workspace:<rel>")
    match root:
        case "home":
            return trial.home / rel
        case "workspace":
            return trial.workspace / rel
        case _:
            raise ManifestError(f"config path {path!r} must be home:<rel> or workspace:<rel>")


def dump_config(fmt: Literal["json", "toml", "yaml"], content: JsonObject) -> bytes:
    match fmt:
        case "json":
            return (json.dumps(content, indent=2) + "\n").encode()
        case "toml":
            return dump_toml(content).encode()
        case "yaml":
            return yaml.safe_dump(content, sort_keys=False).encode()
        case _:
            assert_never(fmt)


async def write_configs(files: tuple[ConfigFile, ...], trial: Trial, box: BoxHandle) -> None:
    values = trial_values(trial)
    for f in files:
        world = Capability.MCP in trial.requires or not f.requires_mcp
        desktop = Capability.DESKTOP in trial.requires and f.desktop_content is not None
        if not (world or desktop):
            continue
        # A desktop trial without the world still gets the file, holding the desktop alone.
        content = render_json(f.content, values) if world else {}
        if not isinstance(content, dict):
            raise ManifestError(f"{f.path}: content must render to an object")
        if Capability.MCP in trial.requires and f.mcp_content is not None:
            mcp_content = render_json(f.mcp_content, values)
            if not isinstance(mcp_content, dict):
                raise ManifestError(f"{f.path}: mcp_content must render to an object")
            content = {**content, **mcp_content}
        if desktop:
            desktop_content = render_json(f.desktop_content, values)
            if not isinstance(desktop_content, dict):
                raise ManifestError(f"{f.path}: desktop_content must render to an object")
            content = merged(content, desktop_content)
        await box.write_file(config_target(f.path, trial), dump_config(f.format, content))


def merged(base: JsonObject, extra: JsonObject) -> JsonObject:
    """`base` with `extra` laid over it, objects merged key by key, so a desktop server joins the world's
    `mcpServers` rather than replacing it."""
    out = dict(base)
    for key, value in extra.items():
        current = out.get(key)
        out[key] = merged(current, value) if isinstance(current, dict) and isinstance(value, dict) else value
    return out


# ---------------------------------------------------------------------------
# TOML output (stdlib reads TOML but cannot write it)
# ---------------------------------------------------------------------------


def toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key)


def toml_value(value: JsonValue, where: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # JSON string escapes are valid TOML basic-string escapes
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v, where) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{toml_key(k)} = {toml_value(v, where)}" for k, v in value.items()) + "}"
    raise ManifestError(f"{where}: TOML has no null")


def dump_toml(table: JsonObject, prefix: tuple[str, ...] = ()) -> str:
    """Scalars and arrays first, then each sub-table as its own [section]."""
    where = ".".join(prefix) or "<root>"
    lines = [f"{toml_key(k)} = {toml_value(v, where)}" for k, v in table.items() if not isinstance(v, dict)]
    out = []
    if prefix and (lines or not any(isinstance(v, dict) for v in table.values())):
        out.append("[" + ".".join(toml_key(p) for p in prefix) + "]")
    out.extend(lines)
    body = "\n".join(out)
    for k, v in table.items():
        if isinstance(v, dict):
            sub = dump_toml(v, (*prefix, k))
            body = f"{body}\n\n{sub}" if body else sub
    return body + ("\n" if not prefix else "")


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class ManifestHarness(SubprocessHarness):
    """Runs a `protocol = "cli"` manifest: one CLI invocation per turn."""

    def __init__(self, spec: ManifestSpec) -> None:
        if spec.protocol != "cli" or spec.parser is None:
            raise ManifestError(f"{spec.info.name}: ManifestHarness runs cli manifests with a parser")
        super().__init__(spec.info)
        self.spec = spec
        self.parser_id = spec.parser
        self.preassigned_session = spec.session == "preassigned"

    @property
    def config_hash(self) -> str:
        return config_hash(self.spec)

    @property
    def tb2_container(self) -> str | None:
        """The owned task container when this trial runs in TB2 mode, else None."""
        if self._trial is None or not self.spec.tb2_argv_first:
            return None
        return self._trial.task_container

    def tb2_values(self) -> dict[str, str]:
        """Placeholder values inside the task container: `/app` is its workdir, `/root` its home."""
        return {**trial_values(self.trial), "workspace": "/app", "home": "/root"}

    async def _exec_container(
        self,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 900.0,
    ) -> tuple[int | None, str, str]:
        container = self.tb2_container
        if container is None:
            raise ManifestError(f"{self.spec.info.name}: no task container for this trial")
        docker = ["docker", "exec", "-i", "-u", "0"]
        for key, value in (env or {}).items():  # credentials travel per exec as env, never to disk
            docker += ["-e", f"{key}={value}"]
        docker += [container, *argv]
        process = await asyncio.create_subprocess_exec(
            *docker, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        return process.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def _write_container_file(self, target: str, data: bytes) -> None:
        script = f"mkdir -p {shlex.quote(str(Path(target).parent))} && cat > {shlex.quote(target)}"
        code, _out, err = await self._exec_container(["sh", "-c", script], stdin=data)
        if code != 0:
            raise ManifestError(
                f"{self.spec.info.name}: writing {target} in the task container failed: {err.strip()[-300:]}"
            )

    async def write_config(self, trial: Trial) -> None:
        if self.tb2_container is None:
            await write_configs(self.spec.config, trial, self.box)
            return
        # Harbor-style setup: the declared install script runs as root in the owned container,
        # then the rendered config files are written inside it (home: -> /root, workspace: -> /app).
        if self.spec.tb2_install.strip():
            code, _out, err = await self._exec_container(["sh", "-c", self.spec.tb2_install])
            if code != 0:
                raise ManifestError(
                    f"{self.spec.info.name}: tb2 install failed in the task container (exit {code}): {err.strip()[-300:]}"
                )
        values = self.tb2_values()
        for f in self.spec.config:
            world = Capability.MCP in trial.requires or not f.requires_mcp
            desktop = Capability.DESKTOP in trial.requires and f.desktop_content is not None
            if not (world or desktop):
                continue
            content = render_json(f.content, values) if world else {}
            if not isinstance(content, dict):
                raise ManifestError(f"{f.path}: content must render to an object")
            if Capability.MCP in trial.requires and f.mcp_content is not None:
                mcp_content = render_json(f.mcp_content, values)
                if not isinstance(mcp_content, dict):
                    raise ManifestError(f"{f.path}: mcp_content must render to an object")
                content = {**content, **mcp_content}
            if desktop:
                desktop_content = render_json(f.desktop_content, values)
                if not isinstance(desktop_content, dict):
                    raise ManifestError(f"{f.path}: desktop_content must render to an object")
                content = merged(content, desktop_content)
            root, _sep, rel = f.path.partition(":")
            base = "/root" if root == "home" else "/app"
            await self._write_container_file(f"{base}/{rel}", dump_config(f.format, content))

    async def turn(self, t: TurnInput) -> TurnOutput:
        if self.tb2_container is None:
            return await super().turn(t)
        if t.new_session and self.in_session:
            self.new_session()
        if self.in_session and self.session_id is None:
            return TurnOutput(
                status=TurnStatus.HARNESS_ERROR, answer="", session_id=None,
                error="previous turn reported no session id, so this turn cannot continue it",
            )  # fmt: skip
        prompt_file = f"/tmp/parallax-turn-{t.index:02d}.md"
        await self._write_container_file(prompt_file, t.text.encode())
        values = {**self.tb2_values(), "prompt": t.text, "prompt_file": prompt_file}
        if self.session_id is not None:
            values["session"] = self.session_id
        templates = self.spec.tb2_argv_next if self.in_session and self.spec.tb2_argv_next else self.spec.tb2_argv_first
        argv = [render(a, values) for a in templates]
        env = {**self.trial.env, **{k: render(v, self.tb2_values()) for k, v in self.spec.env.items()}}
        code, out, err = await self._exec_container(argv, stdin=self.stdin(t), env=env, timeout=7200.0)
        self.stderr_tail = err[-STDERR_TAIL_CHARS:]
        self.output_tail.add(f"=== turn {t.index} exit {code} ===\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n")
        self.in_session = True
        parsed = self.stamp(self.parse(out, err), t.index)
        self.session_id = parsed.session_id or self.session_id
        if code != 0 and parsed.status is TurnStatus.COMPLETED:
            last = err.strip().splitlines()[-1:] or ["no stderr"]
            return parsed.model_copy(update={"status": TurnStatus.HARNESS_ERROR, "error": f"exit {code}: {last[0]}"})
        return parsed

    def turn_values(self, turn: TurnInput, session_id: str | None, prompt_file: Path) -> dict[str, str]:
        values = {**trial_values(self.trial), "prompt": turn.text, "prompt_file": str(prompt_file)}
        if session_id is not None:
            values["session"] = session_id
        return values

    def argv(self, turn: TurnInput, *, continuing: bool, session_id: str | None, prompt_file: Path) -> list[str]:
        values = self.turn_values(turn, session_id, prompt_file)
        argv = [render(a, values) for a in (self.spec.argv_next if continuing else self.spec.argv_first)]
        if Capability.MCP not in self.trial.requires and self.spec.mcp_argv:
            mcp_argv = [render(a, values) for a in self.spec.mcp_argv]
            for index in range(len(argv) - len(mcp_argv) + 1):
                if argv[index : index + len(mcp_argv)] == mcp_argv:
                    argv = [*argv[:index], *argv[index + len(mcp_argv) :]]
                    break
            else:
                raise ManifestError(f"{self.spec.info.name}: mcp_argv is absent from rendered argv")
        return argv

    def env(self) -> dict[str, str]:
        values = trial_values(self.trial)
        return {**self.trial.env, **{k: render(v, values) for k, v in self.spec.env.items()}}

    def stdin(self, turn: TurnInput) -> bytes | None:
        match self.spec.stdin:
            case "prompt":
                return turn.text.encode()
            case "none":
                return None
            case _:
                assert_never(self.spec.stdin)

    def parse(self, stdout: str, stderr: str) -> TurnOutput:
        return PARSERS[self.parser_id](stdout, stderr)


def harness_from_manifest(name: str) -> Harness:
    return ManifestHarness(load_manifest(manifest_path(name)))
