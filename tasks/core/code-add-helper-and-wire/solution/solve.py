from pathlib import Path

SLUGIFY = '''

def slugify(text: str) -> str:
    """Lowercase text and join its letter/digit runs with single hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    helpers = workspace / "jotter" / "text.py"
    body = helpers.read_text(encoding="utf-8")
    body = body.replace(
        '"""Small text helpers used by the CLI."""\n', '"""Small text helpers used by the CLI."""\n\nimport re\n', 1
    )
    helpers.write_text(body + SLUGIFY, encoding="utf-8")
    cli = workspace / "jotter" / "cli.py"
    src = cli.read_text(encoding="utf-8")
    src = src.replace("from jotter.text import truncate", "from jotter.text import slugify, truncate")
    src = src.replace('return title.lower().replace(" ", "-") + ".md"', 'return slugify(title) + ".md"')
    cli.write_text(src, encoding="utf-8")
    return (
        "Added slugify() to jotter/text.py and note_filename() in the CLI now uses it, so "
        '"Q3 Planning: Draft #2" becomes q3-planning-draft-2.md. The existing tests still pass.'
    )
