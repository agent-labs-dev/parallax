"""jotter: a tiny notes CLI. `new TITLE` creates a note, `list` shows them."""

import argparse
from pathlib import Path

from jotter.text import truncate


def note_filename(title: str) -> str:
    return title.lower().replace(" ", "-") + ".md"


def cmd_new(args: argparse.Namespace) -> int:
    folder = Path(args.dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / note_filename(args.title)
    path.write_text(f"# {args.title}\n", encoding="utf-8")
    print(path)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    for path in sorted(Path(args.dir).glob("*.md")):
        first = path.read_text(encoding="utf-8").splitlines()[0].removeprefix("# ")
        print(f"{path.name}\t{truncate(first, args.width)}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jotter")
    parser.add_argument("--dir", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new")
    new.add_argument("title")
    new.set_defaults(func=cmd_new)
    ls = sub.add_parser("list")
    ls.add_argument("--width", type=int, default=40)
    ls.set_defaults(func=cmd_list)
    args = parser.parse_args(argv)
    return args.func(args)
