"""Declarative harnesses: a TOML `ManifestSpec` run as a CLI, one process per turn.

Placeholders are substituted into argv elements, env values and config-file strings (keys too, since
some configs key maps by model id). Substitution is typed and never passes through a shell: a
`{prompt}` element becomes exactly one argv element holding the prompt verbatim.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, assert_never

import yaml
from pydantic import JsonValue, ValidationError

from parallax.harnesses.base import SubprocessHarness, config_hash, credential_env_problems
from parallax.harnesses.parsers import PARSERS
from parallax.models import Capability, ConfigFile, JsonObject, ManifestSpec, Trial, TurnInput, TurnOutput
from parallax.protocols import BoxHandle, Harness

MANIFEST_DIR = Path(__file__).parent / "manifests"

PLACEHOLDERS = frozenset({
    "model", "model_provider", "model_name", "prompt", "prompt_file", "session", "workspace", "home",
    "world_url", "world_token", "world_token_env", "meter_openai", "meter_anthropic", "meter_key",
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
    for text in (*spec.argv_first, *spec.argv_next, *spec.env.values(), *config_strings(spec.config)):
        problems.extend(f"unknown placeholder {{{p}}}" for p in _PLACEHOLDER.findall(text) if p not in PLACEHOLDERS)
    problems.extend(cli_problems(spec))
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
        if Capability.MCP not in trial.requires and f.requires_mcp:
            continue
        content = render_json(f.content, values)
        if not isinstance(content, dict):
            raise ManifestError(f"{f.path}: content must render to an object")
        if Capability.MCP in trial.requires and f.mcp_content is not None:
            mcp_content = render_json(f.mcp_content, values)
            if not isinstance(mcp_content, dict):
                raise ManifestError(f"{f.path}: mcp_content must render to an object")
            content = {**content, **mcp_content}
        await box.write_file(config_target(f.path, trial), dump_config(f.format, content))


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

    async def write_config(self, trial: Trial) -> None:
        await write_configs(self.spec.config, trial, self.box)

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
