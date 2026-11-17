"""`parallax.match` compares structured state; an answer's verdict comes from the judge, never from it.

Its matchers read a simulated world's rows and arguments (`world/`) and a verifier command's stdout
(`semantic.literal_result`, which only routes a question), and `task.py` parses datetimes for its lint. Any other module importing `parallax.match`, in any
spelling, is answer-side code, and must ask the judge instead.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2] / "parallax"
ALLOWED = ("world/", "semantic.py", "task.py")


def imports_match(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(a.name.split(".")[:2] == ["parallax", "match"] for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module is not None and node.module.split(".")[:2] == ["parallax", "match"]:
                return True
            if node.module == "parallax" and any(a.name == "match" for a in node.names):
                return True
    return False


def test_the_guard_sees_every_import_spelling() -> None:
    for source in (
        "import parallax.match",
        "import parallax.match as m",
        "from parallax.match import match_fields",
        "from parallax.match import (\n    match,\n    normalise,\n)",
        "from parallax.match import match as is_match",
        "from parallax import match",
        "from parallax import (\n    match as m,\n)",
    ):
        assert imports_match(source), source
    assert not imports_match("from parallax import models\nimport match")


def test_only_state_code_uses_the_matchers() -> None:
    users = [
        str(path.relative_to(ROOT))
        for path in sorted(ROOT.rglob("*.py"))
        if path.name != "match.py" and imports_match(path.read_text())
    ]
    assert users and all(user.startswith(ALLOWED) for user in users), users
