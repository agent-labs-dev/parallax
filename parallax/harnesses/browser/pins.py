"""Check browser competitor pins against their current public releases.

This command is intentionally separate from offline evaluation. It performs
network reads only when a maintainer explicitly asks for ``check`` or ``update``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

PINS_PATH = Path(__file__).with_name("pins.json")


def load() -> dict[str, dict[str, str]]:
    return json.loads(PINS_PATH.read_text(encoding="utf-8"))


def latest(http: httpx.Client, name: str, registry: str) -> str:
    if registry == "pypi":
        response = http.get(f"https://pypi.org/pypi/{name}/json")
        response.raise_for_status()
        return str(response.json()["info"]["version"])
    if registry == "npm":
        response = http.get(f"https://registry.npmjs.org/{name}/latest")
        response.raise_for_status()
        return str(response.json()["version"])
    raise ValueError(f"{name}: unknown registry {registry!r}")


def stale(http: httpx.Client, names: list[str] | None = None) -> list[dict[str, Any]]:
    entries = [{"name": name, **pin} for name, pin in load().items()]
    if names is not None:
        wanted = set(names)
        entries = [entry for entry in entries if entry["name"] in wanted]
    behind = []
    for entry in entries:
        newest = latest(http, entry["name"], entry["registry"])
        if newest != entry["version"]:
            behind.append({**entry, "latest": newest})
    return behind


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m parallax.harnesses.browser.pins")
    parser.add_argument("action", choices=("check", "update"))
    parser.add_argument("--packages", nargs="+")
    args = parser.parse_args(argv)
    with httpx.Client(timeout=30, follow_redirects=True) as http:
        behind = stale(http, args.packages)
    for entry in behind:
        print(f"{entry['name']} ({entry['registry']}): pinned {entry['version']}, latest {entry['latest']}")
    if args.action == "check":
        if behind:
            print("update pins.json before a comparison", file=sys.stderr)
        return 1 if behind else 0
    pins = load()
    for entry in behind:
        pins[entry["name"]]["version"] = entry["latest"]
    PINS_PATH.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
