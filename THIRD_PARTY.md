# Data and asset provenance

Parallax source code uses MIT (`LICENSE`). Authored task data uses CC BY 4.0
(`LICENSE-DATA`). Third-party materials retain their own licenses below.

## Task fixtures

The bundled core task fixtures are Parallax-authored. These fixtures contain
fictional data:

| File | Content |
|---|---|
| `tasks/core/files-transport-summary/environment/files/journeys-by-type.csv` | Invented transport figures |
| `tasks/core/files-brief-from-packet/environment/files/research-packet.md` | A fictional Harbourton council monitoring packet |
| `tasks/core/files-revise-deck-html/environment/files/deck.html` | A fictional Northstar company presentation |

New third-party task data requires a redistribution license, exact source URL,
retrieval date, committed SHA-256 and a `SOURCE.md` beside the task. Unconfirmed
rights block inclusion in a release.

## Branding

Repository branding is excluded from Python source distributions and wheels.
The Parallax mark and banner are original vector artwork. Wordmark outlines
use Roboto Bold 3.016, copyright 2011 The Roboto Project Authors, under the
SIL Open Font License 1.1. Its notice is preserved in the repository at
`assets/brand/OFL-Roboto.txt`. The font binary is not bundled. Asset sources
and regeneration instructions are in the repository's `assets/brand/README.md`.

## Protocol fixtures

Captured harness protocol fixtures record runtime messages for offline contract
tests. They are not copies of the corresponding runtime implementations.
