"""`parallax.match` compares structured state; an answer's verdict comes from the judge, never from it.

Its matchers read a simulated world's rows and arguments (`world/`) and a verifier command's stdout
(`semantic.literal_result`, which only routes a question). Any other module reaching `match` or `match_fields`
is answer-side code, and must ask the judge instead.
"""

from pathlib import Path

ROOT = Path(__file__).parents[2] / "parallax"
ALLOWED = ("world/", "semantic.py")


def test_only_state_code_uses_the_matchers() -> None:
    users = [
        str(path.relative_to(ROOT))
        for path in sorted(ROOT.rglob("*.py"))
        if path.name != "match.py"
        and any(f"from parallax.match import {name}" in path.read_text() for name in ("match", "match_fields"))
    ]
    assert users and all(user.startswith(ALLOWED) for user in users), users
