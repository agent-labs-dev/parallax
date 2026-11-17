"""The pinned browser-harness CLI and coding agent a browser-harness trial runs, installed by Parallax itself.

The worker runs on the host, so what is on the host's PATH is not what a comparison pinned. Each tool goes into
Parallax's cache at its version from pins.json: browser-harness with uv, the agent CLI with npm. The skill the
agent reads is the SKILL.md inside that installed package, found by importing it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from parallax.harnesses.base import HarnessError
from parallax.harnesses.browser import pins
from parallax.harnesses.browser.browser_harness_worker import Agent

SKILL_PACKAGE = "browser-harness"


@dataclass(frozen=True)
class Toolchain:
    agent_bin: Path
    """The agent CLI the worker launches."""
    bin_dir: Path
    """Holds the browser-harness CLI; the worker puts it first on the agent's PATH."""
    skill: Path
    """browser-harness's own SKILL.md."""
    version: str

    def request(self) -> dict[str, str]:
        return {"agent_bin": str(self.agent_bin), "bin_dir": str(self.bin_dir), "skill": str(self.skill)}


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


async def provision(agent: Agent) -> Toolchain:
    """Install, or reuse, the pinned tools for one agent and report their versions."""
    uv, npm = shutil.which("uv"), shutil.which("npm")
    if uv is None or npm is None:
        raise HarnessError("browser-harness needs uv and npm on PATH to install its pinned tools")
    pinned = pins.load()
    harness_version = pinned[SKILL_PACKAGE]["version"]
    package, binary = npm_package(agent)
    agent_version = pinned[package]["version"]
    root = cache_root()

    tools = root / f"{SKILL_PACKAGE}-{harness_version}"
    tool_env = os.environ | {"UV_TOOL_DIR": str(tools / "tools"), "UV_TOOL_BIN_DIR": str(tools / "bin")}
    await _run(uv, "tool", "install", "--quiet", f"{SKILL_PACKAGE}=={harness_version}", env=tool_env)
    python = tools / "tools" / SKILL_PACKAGE / "bin" / "python"
    skill = Path(
        await _run(
            python, "-I", "-c", "import browser_harness, pathlib; "
            "print(pathlib.Path(browser_harness.__file__).with_name('SKILL.md'))",
        )
    )  # fmt: skip
    if not skill.is_file():
        raise HarnessError(f"{SKILL_PACKAGE}=={harness_version} ships no SKILL.md at {skill}")

    prefix = root / f"{binary}-{agent_version}"
    spec = f"{package}@{agent_version}"
    # npm 12 skips a dependency's install scripts unless its project allows them, and Claude Code's postinstall
    # places its native binary; the allowance names exactly the pinned version. Earlier npm runs them anyway.
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / "package.json").write_text(json.dumps({"private": True, "allowScripts": {spec: True}}))
    await _run(npm, "install", "--prefix", prefix, "--no-save", "--no-audit", "--no-fund", spec)
    agent_bin = prefix / "node_modules" / ".bin" / binary

    versions = (
        f"{SKILL_PACKAGE} {await _run(tools / 'bin' / SKILL_PACKAGE, '--version')}",
        f"{agent} {await _run(agent_bin, '--version')}",
    )
    return Toolchain(agent_bin=agent_bin, bin_dir=tools / "bin", skill=skill, version="; ".join(versions))
