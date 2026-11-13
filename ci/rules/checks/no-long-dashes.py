"""Written copy (docs, code, workflows) uses no em or en dashes.

Write a hyphen, comma or colon instead. Task content under tasks/ and licence texts are data and are not checked.
"""

from pathlib import Path

DASHES = ("\u2013", "\u2014")
SKIP = ("tasks/", "LICENSE")
SUFFIXES = (".md", ".py", ".toml", ".yml", ".yaml", ".sh")


def check(files: list[str]) -> list[tuple[str, int, str]]:
    hits = []
    for path in files:
        if path.startswith(SKIP) or not path.endswith(SUFFIXES):
            continue
        for number, line in enumerate(Path(path).read_text(errors="replace").splitlines(), 1):
            if any(d in line for d in DASHES):
                hits.append((path, number, "an em or en dash in written copy; write a hyphen, comma or colon"))
    return hits
