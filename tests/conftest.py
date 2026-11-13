"""Shared test configuration for the v2 suites (tests/core, tests/world, tests/harnesses)."""

from __future__ import annotations

import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest


@cache
def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=False).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    docker_items = [item for item in items if item.get_closest_marker("docker")]
    if docker_items and not docker_available():
        skip = pytest.mark.skip(reason="`docker info` failed: no usable docker daemon")
        for item in docker_items:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def offline_judges(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tests must opt into fake endpoints explicitly; never spend inherited credentials."""
    for key in ("OPENROUTER_API_KEY", "PARALLAX_JUDGE_API_KEY", "PARALLAX_JUDGE_MODELS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
