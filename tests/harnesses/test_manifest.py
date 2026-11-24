"""ManifestHarness end to end against a fake CLI on the host, plus rendering and config output."""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from parallax.box import HostBox
from parallax.harnesses.base import (
    HarnessError,
    credential_env_problems,
    native_credentials,
)
from parallax.harnesses.manifest import (
    ManifestError,
    ManifestHarness,
    dump_config,
    load_manifest,
    manifest_names,
    manifest_path,
    manifest_problems,
    render,
    write_configs,
)
from parallax.models import (
    Capability,
    ConfigFile,
    CostSource,
    HarnessInfo,
    HarnessKind,
    Isolation,
    LlmEndpoint,
    ManifestSpec,
    McpEndpoint,
    ParserId,
    Persona,
    Tb2Artifact,
    Trial,
    TurnInput,
    TurnStatus,
)

# Speaks hermes quiet mode: the reply on stdout, `session_id: <id>` on stderr. It echoes its argv and
# stdin, and reports a fresh session id or the one it resumed.

FAKE_CLI = r"""
import json, os, pathlib, sys
mode, rest = sys.argv[1], sys.argv[2:]
stdin = sys.stdin.read()
home = pathlib.Path(os.environ["HOME"])
session = rest[0] if mode == "resume" else "session-" + str(len(list(home.glob("session-*"))))
(home / session).touch()
print(json.dumps({"argv": sys.argv[1:], "stdin": stdin, "meter": os.environ.get("PARALLAX_METER_KEY")}))
print("session_id: " + session, file=sys.stderr)
print("--- hermes session export ---")
print(json.dumps({"id": session, "messages": [{"role": "user", "content": stdin}]}))
"""


def info(name: str = "fake") -> HarnessInfo:
    return HarnessInfo(
        name=name,
        kind=HarnessKind.OPEN,
        capabilities=frozenset({Capability.MULTI_TURN}),
        pins_model=True,
        cost_source=CostSource.METERED,
        version_cmd=(sys.executable, "--version"),
    )


def make_trial(box: HostBox) -> Trial:
    return Trial(
        trial_id="t1",
        task_id="saas-demo",
        model="openai/gpt-5.1",
        mcp=McpEndpoint(url="http://world.test/mcp", stdio=("parallax", "world", "bridge")),
        workspace=box.workspace,
        home=box.home,
        llm=LlmEndpoint(
            openai_base_url="http://meter.test/openai/v1",
            anthropic_base_url="http://meter.test/anthropic",
            relay_url="http://meter.test/relay",
            api_key="px-key",
        ),
        env={"PARALLAX_WORLD_TOKEN": "world-tok"},
        user=Persona(name="Maya Patel", email="maya@example.test", timezone="America/New_York"),
        now=datetime(2026, 3, 2, 9, tzinfo=UTC),
        requires=frozenset(),
    )


@pytest.fixture
def box(tmp_path: Path) -> HostBox:
    return HostBox(tmp_path, Isolation.HOST)


@pytest.fixture
def fake_spec(tmp_path: Path) -> ManifestSpec:
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI)
    return ManifestSpec(
        info=info(),
        protocol="cli",
        parser=ParserId.HERMES_QUIET,
        argv_first=(sys.executable, str(script), "first", "--model", "{model_name}", "{prompt}"),
        argv_next=(sys.executable, str(script), "resume", "{session}", "{prompt}"),
        env={"PARALLAX_METER_KEY": "{meter_key}"},
        config=(
            ConfigFile(
                path="home:.fake/config.toml",
                format="toml",
                content={"model": "{model}", "mcp": {"world": {"url": "{world_url}"}}},
            ),
        ),
    )


def reply(answer: str) -> dict[str, object]:
    return json.loads(answer)


async def test_prompt_is_one_verbatim_argv_element(box: HostBox, fake_spec: ManifestSpec) -> None:
    harness = ManifestHarness(fake_spec)
    await harness.start(make_trial(box), box)
    prompt = 'post "hi" to #conf; rm -rf $HOME && echo `id` {model}'
    out = await harness.turn(TurnInput(index=1, text=prompt, new_session=False))
    assert out.status is TurnStatus.COMPLETED
    body = reply(out.answer)
    assert body["argv"] == ["first", "--model", "gpt-5.1", prompt]
    assert body["stdin"] == prompt
    assert body["meter"] == "px-key"
    assert (box.home / "turns" / "01.md").read_text() == prompt


async def test_continuation_resumes_session(box: HostBox, fake_spec: ManifestSpec) -> None:
    harness = ManifestHarness(fake_spec)
    await harness.start(make_trial(box), box)
    first = await harness.turn(TurnInput(index=1, text="one", new_session=False))
    second = await harness.turn(TurnInput(index=2, text="two", new_session=False))
    assert reply(second.answer)["argv"] == ["resume", first.session_id, "two"]
    assert second.session_id == first.session_id
    assert {s.turn for s in second.steps} == {2}


async def test_new_session_starts_fresh(box: HostBox, fake_spec: ManifestSpec) -> None:
    harness = ManifestHarness(fake_spec)
    await harness.start(make_trial(box), box)
    first = await harness.turn(TurnInput(index=1, text="one", new_session=False))
    fresh = await harness.turn(TurnInput(index=2, text="two", new_session=True))
    assert reply(fresh.answer)["argv"][0] == "first"
    assert fresh.session_id != first.session_id


async def test_tb2_exec_uses_manifest_env_allowlist(box: HostBox, fake_spec: ManifestSpec) -> None:
    spec = fake_spec.model_copy(
        update={
            "tb2_argv_first": (sys.executable, "-c", "print('done')"),
            "info": info().model_copy(update={"capabilities": frozenset({Capability.LIVE_CONTAINER})}),
        }
    )
    harness = ManifestHarness(spec)
    trial = make_trial(box).model_copy(
        update={"task_container": "owned-container", "env": {"PARALLAX_WORLD_TOKEN": "secret"}}
    )
    harness._trial = trial
    calls: list[dict[str, str]] = []

    async def fake_write(_target: str, _data: bytes) -> None:
        return None

    async def fake_exec(_argv: list[str], *, stdin=None, env=None, timeout=900.0):
        calls.append(dict(env or {}))
        return 0, json.dumps({"argv": [], "stdin": ""}), "session_id: s1\n"

    harness._write_container_file = fake_write  # type: ignore[method-assign]
    harness._exec_container = fake_exec  # type: ignore[method-assign]
    await harness.turn(TurnInput(index=1, text="hi", new_session=False))
    assert calls[-1] == {"PARALLAX_METER_KEY": "px-key"}
    assert "PARALLAX_WORLD_TOKEN" not in calls[-1]


async def test_tb2_install_copies_host_verified_artifacts_before_install(
    box: HostBox, fake_spec: ManifestSpec, tmp_path: Path
) -> None:
    artifact = Tb2Artifact(name="node.tar.xz", url="https://node.invalid/node.tar.xz", digest="0" * 64)
    spec = fake_spec.model_copy(
        update={
            "tb2_install": "install-artifacts",
            "tb2_artifacts": (artifact,),
            "tb2_argv_first": ("sh", "-c", "true"),
            "info": info().model_copy(update={"capabilities": frozenset({Capability.LIVE_CONTAINER})}),
        }
    )
    harness = ManifestHarness(spec)
    trial = make_trial(box).model_copy(update={"task_container": "owned-container"})
    harness._trial = trial
    order: list[tuple[str, str]] = []

    async def fake_fetch(_artifact: Tb2Artifact) -> Path:
        order.append(("fetch", _artifact.name))
        return tmp_path / _artifact.name

    async def fake_copy(_source: Path, name: str) -> None:
        order.append(("copy", name))

    async def fake_exec(argv: list[str], **_kwargs: object) -> tuple[int, str, str]:
        order.append(("install", argv[2]))
        return 0, "", ""

    async def fake_write(_target: str, _data: bytes) -> None:
        return None

    harness._fetch_artifact = fake_fetch  # type: ignore[method-assign]
    harness._copy_artifact = fake_copy  # type: ignore[method-assign]
    harness._exec_container = fake_exec  # type: ignore[method-assign]
    harness._write_container_file = fake_write  # type: ignore[method-assign]
    await harness.write_config(trial)
    assert order[:3] == [("fetch", "node.tar.xz"), ("copy", "node.tar.xz"), ("install", "install-artifacts")]


async def test_config_rendered_into_home(box: HostBox, fake_spec: ManifestSpec) -> None:
    harness = ManifestHarness(fake_spec)
    await harness.start(make_trial(box), box)
    written = tomllib.loads((box.home / ".fake" / "config.toml").read_text())
    assert written == {"model": "openai/gpt-5.1", "mcp": {"world": {"url": "http://world.test/mcp"}}}


async def test_config_path_cannot_escape_home(box: HostBox) -> None:
    files = (ConfigFile(path="home:../escape.json", format="json", content={}),)
    with pytest.raises(ManifestError):
        await write_configs(files, make_trial(box), box)


def test_render_rejects_unknown_and_missing_placeholders() -> None:
    with pytest.raises(ManifestError, match="unknown"):
        render("{nope}", {})
    with pytest.raises(ManifestError, match="no value"):
        render("{meter_key}", {})


def test_dump_config_formats_round_trip() -> None:
    content = {"a": 1, "flag": True, "list": ["x", "y"], "t": {"k": 'v "q"', "deep": {"n": 2.5}}, "odd key": "z"}
    assert tomllib.loads(dump_config("toml", content).decode()) == content
    assert json.loads(dump_config("json", content)) == content
    assert yaml.safe_load(dump_config("yaml", content)) == content
    with pytest.raises(ManifestError, match="null"):
        dump_config("toml", {"a": None})


@pytest.mark.parametrize("name", manifest_names())
def test_shipped_manifest_is_valid(name: str) -> None:
    spec = load_manifest(manifest_path(name))
    if spec.protocol == "cli":
        ManifestHarness(spec)


def test_native_credentials_forward_only_declared_keys_of_unmetered_harnesses() -> None:
    # No in-tree manifest is unmetered; the format still supports one that authenticates natively.
    gemini = load_manifest(manifest_path("hermes")).info.model_copy(
        update={"cost_source": CostSource.HARNESS, "credential_env": ("GEMINI_API_KEY",)}
    )
    host = {"GEMINI_API_KEY": "g-key", "OPENAI_API_KEY": "o-key", "PARALLAX_UPSTREAM_OPENAI_KEY": "p-key"}
    assert native_credentials(gemini, host) == {"GEMINI_API_KEY": "g-key"}
    with pytest.raises(HarnessError, match="GEMINI_API_KEY"):
        native_credentials(gemini, {})  # declared but unset: it could never authenticate

    metered = gemini.model_copy(update={"cost_source": CostSource.METERED})
    assert credential_env_problems(metered)
    for name in ("OPENAI_API_KEY", "PARALLAX_UPSTREAM_OPENAI_KEY"):
        assert credential_env_problems(gemini.model_copy(update={"credential_env": (name,)})), name


async def test_a_cancelled_turn_kills_its_process(box: HostBox, fake_spec: ManifestSpec, tmp_path: Path) -> None:
    # The runner's `--turn-timeout` cancels the turn; the CLI under it must not outlive that.
    pid_file = tmp_path / "pid"
    hang = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    harness = ManifestHarness(fake_spec.model_copy(update={"argv_first": (sys.executable, "-c", hang)}))
    await harness.start(make_trial(box), box)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(harness.turn(TurnInput(index=1, text="hi", new_session=False)), 1.0)
    stat = Path(f"/proc/{pid_file.read_text()}/stat")
    assert not stat.exists() or stat.read_text().split(") ")[1].startswith("Z")


DESKTOP_CONFIG = ConfigFile(
    path="home:mcp.json",
    format="json",
    requires_mcp=True,
    content={"mcpServers": {"world": {"url": "{world_url}"}}},
    desktop_content={"mcpServers": {"desktop": {"command": "cua-driver", "args": ["mcp"]}}},
)


@pytest.mark.parametrize(
    ("requires", "servers"),
    [
        (frozenset({Capability.MCP, Capability.DESKTOP}), {"world", "desktop"}),
        (frozenset({Capability.DESKTOP}), {"desktop"}),
        (frozenset({Capability.MCP}), {"world"}),
    ],
)
async def test_desktop_content_reaches_every_desktop_trial_beside_the_world_it_uses(
    box: HostBox, requires: frozenset[Capability], servers: set[str]
) -> None:
    trial = make_trial(box).model_copy(update={"requires": requires})
    await write_configs((DESKTOP_CONFIG,), trial, box)
    written = json.loads((box.home / "mcp.json").read_text())
    assert set(written["mcpServers"]) == servers


def test_declaring_desktop_needs_a_config_that_gives_the_harness_the_desktop(fake_spec: ManifestSpec) -> None:
    desktop = fake_spec.info.model_copy(update={"capabilities": frozenset({Capability.DESKTOP})})
    bare = fake_spec.model_copy(update={"info": desktop})
    assert any("desktop_content" in p for p in manifest_problems(bare, "fake"))
    given = bare.model_copy(update={"config": (DESKTOP_CONFIG,)})
    assert not any("desktop_content" in p for p in manifest_problems(given, "fake"))
