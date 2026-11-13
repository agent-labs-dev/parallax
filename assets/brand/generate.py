#!/usr/bin/env python3
"""Regenerate assets/brand/ for Parallax: mark, wordmarks, lockups and PNGs.

Concept: parallax is the apparent shift of one object seen from two lines of
sight. The mark draws a solid accent disc (the object, as the first viewpoint
sees it) and a hollow echo disc offset horizontally (the same object, as the
second viewpoint sees it). Their overlap is the comparison the harness makes.
No orbit rings, no sparkle.

Palette, carried over from assets/banner.svg:
  INK    #14161d   near-black panel
  PAPER  #e9edf3   near-white
  ACCENT #e35ba0   magenta
  MUTED  #8b93a3   slate gray

Prerequisites: fontTools, Pillow (optional, favicon only), rsvg-convert, and
Roboto Bold installed (Arch: ttf-roboto; Debian/Ubuntu: fonts-roboto).

Run:  python3 assets/brand/generate.py [--font /path/to/Roboto-Bold.ttf] [--out DIR]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

OUT = Path(__file__).resolve().parent

# Common Roboto Bold locations, tried in order when --font is not given.
FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/Roboto-Bold.ttf",                      # Arch: ttf-roboto
    "/usr/share/fonts/truetype/roboto/Roboto-Bold.ttf",          # Debian/Ubuntu: fonts-roboto
    "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Bold.ttf",
    "/usr/share/fonts/google/roboto/Roboto-Bold.ttf",
    "/usr/share/fonts/roboto/Roboto-Bold.ttf",
]

INK = "#14161d"
PAPER = "#e9edf3"
ACCENT = "#e35ba0"
MUTED = "#8b93a3"

# Mark geometry, in a 64-unit square.
SOLID_C = (23.0, 32.0)  # solid (accent) disc centre
ECHO_C = (41.0, 32.0)   # echo (outline) disc centre
R = 13.0
ECHO_STROKE = 2.75
ECHO_OPACITY = 0.45

WORDMARK = "Parallax"
WORD_SIZE = 74.0
WORD_BASELINE = 77.0
WORD_TRACKING = -1.5
BOX_H = 100.0


def mark_body(ink: str, accent: str) -> str:
    (sx, sy), (ex, ey) = SOLID_C, ECHO_C
    return (
        f'<circle cx="{ex}" cy="{ey}" r="{R}" fill="none" stroke="{ink}" '
        f'stroke-width="{ECHO_STROKE}" opacity="{ECHO_OPACITY}"/>'
        f'<circle cx="{sx}" cy="{sy}" r="{R}" fill="{accent}"/>'
    )


def svg_open(width: float, height: float, label: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.2f} {height:.2f}" '
        f'role="img" aria-label="{label}"><title>{title}</title>'
    )


def mark_svg(ink: str, accent: str) -> str:
    return svg_open(64, 64, "Parallax mark", "Parallax") + mark_body(ink, accent) + "</svg>\n"


def icon_svg() -> str:
    scale = 7.5
    tx = 256 - 32 * scale
    ty = 256 - 32 * scale
    body = f'<g transform="translate({tx} {ty}) scale({scale})">{mark_body(PAPER, ACCENT)}</g>'
    return (
        svg_open(512, 512, "Parallax", "Parallax")
        + f'<rect width="512" height="512" fill="{INK}"/>{body}</svg>\n'
    )


def outline(font: TTFont, char: str, x: float, baseline: float, size: float) -> tuple[str, float]:
    glyphs = font.getGlyphSet()
    name = font.getBestCmap()[ord(char)]
    scale = size / font["head"].unitsPerEm
    pen = SVGPathPen(glyphs)
    glyphs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, x, baseline)))
    return pen.getCommands(), glyphs[name].width * scale


def text_parts(font: TTFont, text: str, ink: str) -> tuple[float, list[str]]:
    x, parts = 10.0, []
    for ch in text:
        path, advance = outline(font, ch, x, WORD_BASELINE, WORD_SIZE)
        parts.append(f'<path d="{path}" fill="{ink}"/>')
        x += advance + WORD_TRACKING
    width = x - WORD_TRACKING + 10.0
    return width, parts


def wordmark_svg(font: TTFont, ink: str) -> str:
    width, parts = text_parts(font, WORDMARK, ink)
    return svg_open(width, BOX_H, "Parallax", "Parallax") + "".join(parts) + "</svg>\n"


def lockup_svg(font: TTFont, ink: str, accent: str) -> str:
    text_width, parts = text_parts(font, WORDMARK, ink)
    mark_scale = 2.2
    gap = 34.0
    mark_tx = 18.0
    mark_ty = BOX_H / 2 - 32 * mark_scale
    mark = f'<g transform="translate({mark_tx} {mark_ty}) scale({mark_scale})">{mark_body(ink, accent)}</g>'
    # Text paths start at x=10 in wordmark space; shift the group to the lockup origin.
    text_x = mark_tx + 64 * mark_scale + gap
    text_group = f'<g transform="translate({text_x - 10.0} 0)">{"".join(parts)}</g>'
    width = text_x + text_width
    return svg_open(width, BOX_H, "Parallax", "Parallax") + mark + text_group + "</svg>\n"


def png(src: Path, dst: Path, width: int | None = None, height: int | None = None) -> None:
    cmd = ["rsvg-convert", "-o", str(dst)]
    if width is not None:
        cmd += ["-w", str(width)]
    if height is not None:
        cmd += ["-h", str(height)]
    cmd += [str(src)]
    subprocess.run(cmd, check=True)


def resolve_font(override: str | None) -> TTFont:
    candidates = [Path(override)] if override else [Path(p) for p in FONT_CANDIDATES]
    for path in candidates:
        if path.is_file():
            return TTFont(str(path))
    hint = (
        "Roboto Bold not found. Install it (Arch: ttf-roboto, Debian/Ubuntu: fonts-roboto) "
        "or pass its path with --font /path/to/Roboto-Bold.ttf"
    )
    raise SystemExit(f"{hint}. Tried: {', '.join(str(p) for p in candidates)}")


def main(args: argparse.Namespace) -> None:
    out = Path(args.out).resolve() if args.out else OUT
    out.mkdir(parents=True, exist_ok=True)
    font = resolve_font(args.font)

    (out / "mark.svg").write_text(mark_svg(INK, ACCENT))
    (out / "mark-dark.svg").write_text(mark_svg(PAPER, ACCENT))
    (out / "logo.svg").write_text(icon_svg())
    (out / "wordmark.svg").write_text(wordmark_svg(font, INK))
    (out / "wordmark-dark.svg").write_text(wordmark_svg(font, PAPER))
    (out / "logo-lockup.svg").write_text(lockup_svg(font, INK, ACCENT))
    (out / "logo-lockup-dark.svg").write_text(lockup_svg(font, PAPER, ACCENT))

    png(out / "logo.svg", out / "icon-512.png", 512, 512)
    for size in (256, 192, 180, 128, 64, 32, 16):
        png(out / "logo.svg", out / f"icon-{size}.png", size, size)
    png(out / "mark.svg", out / "mark-512.png", 512, 512)
    png(out / "mark.svg", out / "mark-256.png", 256, 256)
    png(out / "mark-dark.svg", out / "mark-dark-512.png", 512, 512)
    png(out / "wordmark.svg", out / "wordmark-800.png", 800)
    png(out / "wordmark-dark.svg", out / "wordmark-dark-800.png", 800)
    png(out / "logo-lockup.svg", out / "logo-lockup-800.png", 800)
    png(out / "logo-lockup-dark.svg", out / "logo-lockup-dark-800.png", 800)

    try:
        from PIL import Image

        img = Image.open(out / "icon-32.png").convert("RGBA")
        img.save(out / "favicon.ico", format="ICO", sizes=[(32, 32), (16, 16)])
    except Exception as exc:  # Pillow optional; PNGs above already cover favicons
        print(f"favicon.ico skipped: {exc}", file=sys.stderr)

    print("wrote", sorted(p.name for p in out.iterdir()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", help="path to Roboto-Bold.ttf")
    parser.add_argument("--out", help="output directory (default: this script's directory)")
    main(parser.parse_args())
