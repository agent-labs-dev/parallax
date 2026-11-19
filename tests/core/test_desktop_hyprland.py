"""Real Hyprland desktop box: needs docker, the host's vkms display and the hyprland image. CI builds it; pytest never does."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from parallax.box import DockerBox
from parallax.task import load_task

TASK = Path(__file__).resolve().parents[2] / "tasks" / "desktop-hyprland" / "files-desktop-hyprland-expense-form"


async def run(box: DockerBox, *argv: str) -> str:
    proc, _ = await box.exec(argv, {}, Path("/workspace"))
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode()


async def test_the_box_runs_a_hyprland_session_with_its_app_and_records_it(tmp_path: Path) -> None:
    image = os.environ.get("PARALLAX_TEST_HYPRLAND_IMAGE")
    if not image:
        pytest.skip("set PARALLAX_TEST_HYPRLAND_IMAGE to a built desktop-hyprland image")
    box = await DockerBox.create_device(load_task(TASK), image)
    try:
        windows = json.loads(await run(box, "deskpilot", "--session", "--json", "windows"))
        assert "Expense Form" in json.dumps(windows)
        assert "WAYLAND_DISPLAY=" in await run(box, "env")
        video = await box.keep_recording(tmp_path / "desktop.mp4")
        assert video is not None and video.stat().st_size > 0
    finally:
        await box.close()
