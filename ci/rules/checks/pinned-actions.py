"""Every GitHub action this repo uses or writes into a consumer's workflow is pinned to a commit SHA.

Pin as `uses: owner/repo@<40 hex> # vX.Y.Z`. Some actions publish no major tags (setup-uv@v10 does not
resolve), and a SHA cannot move under a consumer.
"""

import re
from pathlib import Path

USES = re.compile(r"\buses:\s*([^\s#]+)")
PINNED = re.compile(r"[^@]+@[0-9a-f]{40}")


def check(files: list[str]) -> list[tuple[str, int, str]]:
    hits = []
    for path in files:
        if not (path.startswith(".github/workflows/") or path == "parallax/ci.py"):
            continue
        for number, line in enumerate(Path(path).read_text().splitlines(), 1):
            match = USES.search(line)
            if match and not match[1].startswith("./") and not PINNED.fullmatch(match[1]):
                hits.append((path, number, f"{match[1]} is not pinned to a commit SHA"))
    return hits
