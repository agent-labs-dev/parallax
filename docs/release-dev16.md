# Release 2.0.0.dev16

This release packages the OpenRouter Jev migration from
[PR #56](https://github.com/agent-labs-dev/parallax/pull/56).
Consumers using Jev need `OPENROUTER_API_KEY` and the `NoulAnswer.noul` and
`JevCall.refused` interfaces. See [the judge contract](jev-judge.md).

## Maintainer procedure

1. Review and merge the version PR after its offline CI checks pass.
2. Obtain explicit authorization to publish `2.0.0.dev16`.
3. Create and push the matching signed tag `v2.0.0.dev16` on the merged commit.
4. Watch the `release` workflow. Its build job checks the tag against the wheel
   version and runs the wheel outside the checkout before trusted PyPI publication.
5. Confirm PyPI serves both the `2.0.0.dev16` wheel and source archive. Record
   their SHA-256 hashes from the published artifacts.
6. Resolve consumer lockfiles against those published artifacts, then run each
   consumer's focused offline compatibility checks and CI before merging it.

Preparing this PR does not authorize a tag, publication or merge. The private
repository is not a replacement dependency source for public consumers.
Consumer lock resolution remains blocked until PyPI serves this version.

Existing evaluation receipt files stay unchanged. Jev receipts from dev15 and
earlier need the original package version to deserialize: dev16 uses the noul
and refused wire schema. Opus-only receipts with no Jev call retain their schema. An Opus-only consumer can retain
its existing policy identity only after proving its questions, fallback
instructions, schema, model, endpoint, request body and image handling are
unchanged. Jev-enabled policies must identify the changed transport contract.
