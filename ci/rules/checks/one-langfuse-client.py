"""Only parallax/track/sink.py builds a Langfuse client, and nothing in parallax/ shuts one down.

Get the client from parallax.track.sink.connect() and never shut it down. The SDK caches its workers per
public key and shutdown() leaves the dead entry cached, so a second client in the same process hangs in flush.
"""

import re
from pathlib import Path

SINK = "parallax/track/sink.py"
BUILD = re.compile(r"\bLangfuse\(")
SHUTDOWN = re.compile(r"\.shutdown\(\)")


def check(files: list[str]) -> list[tuple[str, int, str]]:
    hits = []
    for path in files:
        if not (path.startswith("parallax/") and path.endswith(".py")):
            continue
        for number, line in enumerate(Path(path).read_text().splitlines(), 1):
            if BUILD.search(line) and path != SINK:
                hits.append((path, number, "builds a second Langfuse client; use parallax.track.sink.connect()"))
            if SHUTDOWN.search(line):
                hits.append((path, number, "shuts down the shared Langfuse client, which hangs the next flush"))
    return hits
