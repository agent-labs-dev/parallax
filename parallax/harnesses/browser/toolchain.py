"""The pinned browser CLI and coding agent a browser CLI trial runs, installed by Parallax itself.

The worker runs on the host, so what is on the host's PATH is not what a comparison pinned. Each tool goes into
Parallax's cache at its version from pins.json: browser-harness with uv, agent-browser and the agent CLI with npm.
The skill the agent reads is the SKILL.md inside the installed package, so it is the skill of the pinned version.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import assert_never

from parallax.harnesses.base import HarnessError
from parallax.harnesses.browser import pins
from parallax.harnesses.browser.browser_harness_worker import Agent


class Cli(StrEnum):
    """The browser CLI and skill an agent is given."""

    BROWSER_HARNESS = "browser-harness"
    AGENT_BROWSER = "agent-browser"

    def environment(self, cdp_url: str, trial: str) -> dict[str, str]:
        """What attaches the CLI to the trial's browser. browser-harness reads its URL from `BU_CDP_WS`.
        agent-browser reads `AGENT_BROWSER_CDP` for any session, which matters because its skill has the agent name
        its own `--session` on every command, so a connection made beforehand on another session would be ignored
        and the CLI would launch a local Chrome. Its daemon sockets are namespaced to the trial, so that trials
        running side by side never share a daemon."""
        match self:
            case Cli.BROWSER_HARNESS:
                return {"BU_CDP_WS": cdp_url}
            case Cli.AGENT_BROWSER:
                return {"AGENT_BROWSER_CDP": cdp_url, "AGENT_BROWSER_NAMESPACE": trial}
            case _:
                assert_never(self)

    def detach(self) -> list[str]:
        """Run after the browser was observed: every session the agent opened has a daemon, which outlives the
        agent unless closed (and `--all` reaches only this trial's namespace)."""
        match self:
            case Cli.BROWSER_HARNESS:
                return []
            case Cli.AGENT_BROWSER:
                return ["agent-browser", "close", "--all"]
            case _:
                assert_never(self)


@dataclass(frozen=True)
class Toolchain:
    agent_bin: Path
    """The agent CLI the worker launches."""
    bin_dir: Path
    """Holds the browser CLI; the worker puts it first on the agent's PATH."""
    skill: Path
    """The browser CLI's own SKILL.md."""
    version: str
    cli: Cli = Cli.BROWSER_HARNESS

    def request(self) -> dict[str, object]:
        return {
            "agent_bin": str(self.agent_bin), "bin_dir": str(self.bin_dir), "skill": str(self.skill),
            "skill_name": str(self.cli), "detach": self.cli.detach(),
        }  # fmt: skip


def npm_package(agent: Agent) -> tuple[str, str]:
    """The agent's npm package and the binary it installs."""
    match agent:
        case Agent.CODEX:
            return "@openai/codex", "codex"
        case Agent.CLAUDE_CODE:
            return "@anthropic-ai/claude-code", "claude"
        case _:
            assert_never(agent)


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "parallax" / "browser-harness"


async def _run(*argv: str | Path, env: dict[str, str] | None = None) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
    except FileNotFoundError as exc:
        raise HarnessError(f"{argv[0]} is not installed: {exc}") from exc
    out, err = await proc.communicate()
    if proc.returncode != 0:
        command = " ".join(str(part) for part in argv[:4])
        raise HarnessError(f"{command} failed: {err.decode(errors='replace').strip()[-500:]}")
    return out.decode().strip()


async def _install_agent(npm: str, root: Path, package: str, binary: str, version: str) -> Path:
    prefix = root / f"{binary}-{version}"
    spec = f"{package}@{version}"
    # npm 12 skips a dependency's install scripts unless its project allows them, and Claude Code's postinstall
    # places its native binary; the allowance names exactly the pinned version. Earlier npm runs them anyway.
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / "package.json").write_text(json.dumps({"private": True, "allowScripts": {spec: True}}))
    await _run(npm, "install", "--prefix", prefix, "--no-save", "--no-audit", "--no-fund", spec)
    return prefix / "node_modules"


async def _install_cli(cli: Cli, uv: str, npm: str, root: Path, version: str) -> tuple[Path, Path, str]:
    """The CLI's bin directory, its SKILL.md and its reported version."""
    match cli:
        case Cli.BROWSER_HARNESS:
            tools = root / f"{cli}-{version}"
            tool_env = os.environ | {"UV_TOOL_DIR": str(tools / "tools"), "UV_TOOL_BIN_DIR": str(tools / "bin")}
            await _run(uv, "tool", "install", "--quiet", f"{cli}=={version}", env=tool_env)
            python = tools / "tools" / str(cli) / "bin" / "python"
            skill = Path(
                await _run(
                    python, "-I", "-c", "import browser_harness, pathlib; "
                    "print(pathlib.Path(browser_harness.__file__).with_name('SKILL.md'))",
                )
            )  # fmt: skip
            bin_dir = tools / "bin"
            reported = await _run(bin_dir / str(cli), "--version")
        case Cli.AGENT_BROWSER:
            modules = await _install_agent(npm, root, str(cli), str(cli), version)
            skill = modules / str(cli) / "skills" / str(cli) / "SKILL.md"
            bin_dir = modules / ".bin"
            reported = await _run(bin_dir / str(cli), "--version")
        case _:
            assert_never(cli)
    if not skill.is_file():
        raise HarnessError(f"{cli}=={version} ships no SKILL.md at {skill}")
    return bin_dir, skill, reported


async def provision(agent: Agent, cli: Cli = Cli.BROWSER_HARNESS) -> Toolchain:
    """Install, or reuse, the pinned tools for one agent and report their versions."""
    uv, npm = shutil.which("uv"), shutil.which("npm")
    if uv is None or npm is None:
        raise HarnessError(f"{cli} needs uv and npm on PATH to install its pinned tools")
    pinned = pins.load()
    package, binary = npm_package(agent)
    root = cache_root()
    bin_dir, skill, cli_version = await _install_cli(cli, uv, npm, root, pinned[str(cli)]["version"])
    agent_bin = (await _install_agent(npm, root, package, binary, pinned[package]["version"])) / ".bin" / binary
    versions = (
        cli_version if cli_version.startswith(str(cli)) else f"{cli} {cli_version}",
        f"{agent} {await _run(agent_bin, '--version')}",
    )
    return Toolchain(agent_bin=agent_bin, bin_dir=bin_dir, skill=skill, version="; ".join(versions), cli=cli)
