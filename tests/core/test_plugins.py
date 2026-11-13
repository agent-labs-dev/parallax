"""Harness entry points: a duplicate name fails instead of depending on install order."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from parallax.harnesses import base


def test_duplicate_harness_entry_points_fail_instead_of_using_install_order(monkeypatch):
    entries = [SimpleNamespace(name="external", value=target) for target in ("one:Harness", "two:Harness")]
    monkeypatch.setattr(base, "entry_points", lambda **kwargs: entries)
    with pytest.raises(base.HarnessError, match="multiple harness entry points for 'external'"):
        base.load_harness("external")
