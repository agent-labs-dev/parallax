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

## Official browser suite inputs

The following materials are fetched at run time, verified by the recorded digests, and kept in the user
cache. No upstream dataset bytes are committed to this repository.

| Material | Source and pinned revision | Digest | Licence |
|---|---|---|---|
| BU Bench V2 dataset and judge | [browser-use/benchmark](https://github.com/browser-use/benchmark), `77b4df339fe8fc293fb48896cfa1c73dd743af42` | SHA-256 `0c014b056192e261b06f04ed6da6dcf37f2135150cf58c87ea839bd4e150e5c3` | The upstream repository describes WebBench, Mind2Web 2, and BrowseComp inputs as MIT, and the GAIA input as the public validation split with no explicit licence. The upstream repository code is MIT. |
| Online-Mind2Web dataset | [OSU-NLP-Group/Online-Mind2Web](https://github.com/OSU-NLP-Group/Online-Mind2Web), dataset revision `eacad896a84dc5b65e29b0b06e4699ab0544d701` | SHA-256 `7dedf381531d423dc0fc48d21dc1d425655dd75f38b10211a880fa09102f257f`, Git blob `e8a5e5a99f2be9eae14f4e4259bd5af562f80da9` | Dataset CC BY 4.0. The upstream repository code is MIT. |
| Online-Mind2Web judge | [OSU-NLP-Group/Online-Mind2Web](https://github.com/OSU-NLP-Group/Online-Mind2Web), `b422075b059f0bba670ddda168444eb1d326aa45` | Source revision pin | MIT |
