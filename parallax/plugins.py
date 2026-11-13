"""Entry-point loading shared by every Parallax plugin seam. Imports nothing from parallax: models depend on it."""

from __future__ import annotations

import importlib.metadata


def entry_points_in(group: str) -> list[importlib.metadata.EntryPoint]:
    return sorted(importlib.metadata.entry_points(group=group), key=lambda ep: ep.name)
