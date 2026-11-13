# Parallax brand

Visual identity for Parallax, generated from vector geometry and font outlines.

## Concept

Parallax is the apparent shift of a single object seen from two lines of sight.
The mark draws that literally: a solid accent disc, the object as the first
viewpoint sees it, and a hollow echo disc offset horizontally, the same object
as the second viewpoint sees it. Their overlap is the comparison Parallax makes
between agents.

## Palette

The identity uses these colors:

| Token | Hex | Use |
|---|---|---|
| `ink` | `#14161d` | near-black: wordmark text on light, the app-icon panel, the echo disc on light |
| `paper` | `#e9edf3` | near-white: wordmark text on dark, the echo disc on dark |
| `accent` | `#e35ba0` | magenta: the solid disc, the object under comparison |
| `muted` | `#8b93a3` | slate: secondary text in the banner |

## Assets

| File | What it is | Use on |
|---|---|---|
| `mark.svg` | transparent mark, `ink` echo | light surfaces |
| `mark-dark.svg` | transparent mark, `paper` echo | dark surfaces |
| `logo.svg` | 512 square app icon, mark on the `ink` panel | favicon source, app icon |
| `wordmark.svg` | "Parallax" text, `ink`, transparent | light surfaces |
| `wordmark-dark.svg` | "Parallax" text, `paper`, transparent | dark surfaces |
| `logo-lockup.svg` | mark + wordmark, `ink` text | light surfaces |
| `logo-lockup-dark.svg` | mark + wordmark, `paper` text | dark surfaces |
| `icon-{512,256,192,180,128,64,32,16}.png` | raster app icon | favicons, app stores, touch icons |
| `favicon.ico` | 16 + 32 px | browser favicon |
| `mark-*.png`, `wordmark-*.png`, `logo-lockup-*.png` | raster renders | docs, slides |
| `contact-sheet.html` | every variant on light and dark | review |
| `generate.py` | reproducible generator | see below |

## Typography and font attribution

The wordmark is set in **Roboto Bold** (weight 700) and flattened to SVG outline
paths, so downstream consumers do not need the font installed. The font file is
not shipped in this directory, only the glyph outlines.

- Font: Roboto Bold, Roboto family version 3.016.
- License: SIL Open Font License, Version 1.1 (see [`OFL-Roboto.txt`](OFL-Roboto.txt)).
- Copyright: "Copyright 2011 The Roboto Project Authors (https://github.com/googlefonts/roboto-classic)".
- Local source: `ttf-roboto` 3.016-1, `/usr/share/fonts/TTF/Roboto-Bold.ttf`.

## Usage

- Clear space: leave at least one solid-disc height of empty space on every side.
- Minimum sizes: 16 px for the mark, 64 px wide for the wordmark.
- Light surfaces: `mark.svg`, `wordmark.svg`, `logo-lockup.svg`.
- Dark surfaces: `mark-dark.svg`, `wordmark-dark.svg`, `logo-lockup-dark.svg`.
- The solid disc is always `accent`; the echo disc is always hollow at about 45%
  opacity. Do not fill the echo, recolor the wordmark, rotate, stretch, add
  gradients, glows or outlines to the solid disc.

## Regenerate

```sh
python3 assets/brand/generate.py
```

Requires `fontTools`, `Pillow`, `rsvg-convert` and Roboto Bold. Use
`--font /path/to/Roboto-Bold.ttf` when the font is outside the standard locations. Edits to `generate.py` are the single
source of truth for every SVG and PNG here.
