"""Puts each pinned, vendored dependency on sys.path (no network in the build box)."""

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PIN = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([^\s#]+)")

for _line in (_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
    _match = _PIN.match(_line)
    if _match:
        sys.path.insert(0, str(_ROOT / "vendor" / f"{_match[1]}-{_match[2]}"))
