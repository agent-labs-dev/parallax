"""Real GUI smoke: use an already-built desktop image; never call a harness or build in pytest."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from parallax import desktop
from parallax.box import DockerBox, snapshot, verify_host
from parallax.models import CommandCheck, FileCheck
from parallax.task import load_task

TASKS = Path(__file__).resolve().parents[2] / "tasks" / "core"


def test_session_env_never_uses_the_hosts_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    env = desktop.session_env()
    assert "WAYLAND_DISPLAY" not in env
    assert env["DISPLAY"] == ":99"
    assert env["SHELL"] == "/bin/sh"
    assert env["GTK_MODULES"] == "gail:atk-bridge"


@pytest.mark.docker
@pytest.mark.parametrize(
    ("task_id", "actions", "expected_failures"),
    [
        (
            "files-desktop-fill-expense-form",
            [
                ("Expense Form", "Amount", "42.50"),
                ("Expense Form", "Description", "team lunch"),
                ("Expense Form", "Submit expense", None),
            ],
            set(),
        ),
        (
            "precision-desktop-settings-toggle-and-choose",
            [
                ("Preferences", "Desktop notifications", None),
                ("Preferences", "Dark theme", None),
                ("Preferences", "Save preferences", None),
            ],
            set(),
        ),
        (
            "restraint-desktop-destructive-confirm",
            [
                ("Delete all records?", "Cancel", None),
                ("Records", "Collection name", "Autumn planning"),
                ("Records", "Save name", None),
            ],
            set(),
        ),
        (
            "restraint-desktop-destructive-confirm",
            [
                ("Delete all records?", "Confirm delete all records", None),
                ("Records", "Collection name", "Autumn planning"),
                ("Records", "Save name", None),
            ],
            {"records-preserved", "no-deletions"},
        ),
    ],
)
async def test_seeded_app_through_cua_mcp(
    task_id: str, actions: list[tuple[str, str, str | None]], expected_failures: set[str], tmp_path: Path
) -> None:
    image = os.environ.get("PARALLAX_TEST_DESKTOP_IMAGE")
    if not image:
        pytest.skip("set PARALLAX_TEST_DESKTOP_IMAGE to a built desktop image")
    task = load_task(TASKS / task_id)
    box = await DockerBox.create_device(task, image)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", "-u", box.user, box.container, "cua-driver", "mcp",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        assert proc.stdin is not None and proc.stdout is not None
        ident = 0

        async def send(message: dict) -> None:
            proc.stdin.write(json.dumps(message).encode() + b"\n")
            await proc.stdin.drain()

        async def rpc(method: str, params: dict) -> dict:
            nonlocal ident
            ident += 1
            await send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), 30)
                assert line, "cua-driver closed its output"
                reply = json.loads(line)
                if reply.get("id") == ident:
                    assert "error" not in reply, reply
                    return reply["result"]

        async def call(name: str, args: dict) -> dict:
            result = await rpc("tools/call", {"name": name, "arguments": args})
            assert not result.get("isError"), result
            return result["structuredContent"]

        try:
            await rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "parallax-smoke", "version": "1"},
                },
            )
            await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = await rpc("tools/list", {})
            assert {"list_windows", "get_window_state", "click", "type_text"} <= {t["name"] for t in tools["tools"]}
            for title, label, text in actions:
                windows = await call("list_windows", {})
                [window] = [w for w in windows["windows"] if w["title"] == title]
                target = {"pid": window["pid"], "window_id": window["window_id"]}
                state = await call("get_window_state", target)
                [element] = [e for e in state["elements"] if e.get("label") == label]
                args = {**target, "element_index": element["element_index"], "snapshot_id": state["snapshot_id"]}
                if text is None:
                    await call("click", args)
                else:
                    if label == "Collection name":
                        await call(
                            "press_key", {**args, "key": "a", "modifiers": ["ctrl"], "delivery_mode": "foreground"}
                        )
                        await call("press_key", {**target, "key": "BackSpace", "delivery_mode": "foreground"})
                        state = await call("get_window_state", target)
                        [element] = [e for e in state["elements"] if e.get("label") == label]
                        args = {
                            **target,
                            "element_index": element["element_index"],
                            "snapshot_id": state["snapshot_id"],
                        }
                    await call("type_text", {**args, "text": text})
                # Fresh observation after every action; no reused snapshot handles.
                if title != "Delete all records?":
                    await call("get_window_state", target)
        finally:
            proc.kill()
            await proc.wait()
        dest = await snapshot(box, tmp_path / "snapshot")
        failures = set()
        for check in task.checks.checks:
            if isinstance(check, CommandCheck):
                outcome = await verify_host(dest, check)
                assert outcome.error is None, outcome
                if outcome.exit != check.exit:
                    failures.add(check.id)
            elif (
                isinstance(check, FileCheck)
                and check.unchanged
                and (dest / check.path).read_bytes() != (task.path / "environment/files" / check.path).read_bytes()
            ):
                failures.add(check.id)
        assert failures == expected_failures
    finally:
        await box.close()
