# Judging and attribution

Parallax grades semantic quality only through an **LLM judge panel**. There is
no deterministic or heuristic grader. Execution validity is judged separately
from agent quality.

## Grading

`parallax.semantic` sends the user task, scheduled turns, world state and call
results, captured answers/finals, trusted verify output and file snapshots.
Existing check YAML stays readable as requirements; regexes, keywords, lengths
and counts are never executed to assign a quality verdict. Prefer explicit
rubric questions for new tasks.

Each panel model answers every requirement with a reason and yes, no or null,
in a strict JSON schema. A check is decided only when every panel model gives
the same answer; null or disagreement leaves it ungraded. A malformed reply is
retried once with the reply and its validation error sent back to the model.

A request whose estimated prompt cannot fit the pinned model's documented
window is never sent, and an upstream "prompt is too long" refusal is a known
$0 attempt. Either leaves the question unknown with that reason; evidence is
never truncated to fit. Images in the evidence (`data:image/...` strings, such
as a hosted session's screenshots) reach the panel as image content parts,
each distinct image once, referenced from the JSON by `image:sha256:<hex>`, and
are sized by the provider's visual-token rule. An image set over a documented
limit is not sent, never downscaled or dropped, and the reason is recorded.
Missing configuration, disagreement or unavailable evidence leaves the check
ungraded and the trial INVALID. No skipped judge can create a pass.

Evidence is never silently truncated to obtain a score. The current grading
budget is 256 KiB; oversized evidence, binary files and symlink snapshots need
a supported evidence representation before grading. This is an execution limit,
not a judgement about the agent. Same-family judge use remains visible.

The policy version, panel models and endpoint are part of resume identity and
of each run's recorded judge policy, so `parallax compare` never pairs runs
graded by different evaluators unless `--allow-identity-change` is given.

## Configuration

- Panel: `--judge` or `PARALLAX_JUDGE_MODELS`; with neither, `DEFAULT_MODEL` in
  `parallax/judge.py` (DeepSeek V4.1 Flash 1017) once a key can reach it.
- `PARALLAX_JUDGE_API_KEY` defaults to `OPENROUTER_API_KEY`, and
  `PARALLAX_JUDGE_BASE_URL` to OpenRouter. With no key there is no panel, so
  every check stays INVALID and nothing is spent.
- `PARALLAX_JUDGE_PROVIDERS` (comma-separated, in preference order) holds the
  panel to those upstream hosts on the AI Gateway or OpenRouter; any other
  endpoint refuses it. Set it when some of a model's hosts ignore the strict
  reply schema. Unset on OpenRouter, the panel skips the hosts in
  `IGNORED_OPENROUTER_HOSTS`, measured to drop evidence on long calls.
- `parallax check` uses the same judges for oracle, null controls and mutants.
  It makes model calls. `parallax check --lint-only` and CI protocol tests run
  offline.

Only recorded world internal errors and boot failures receive direct
infrastructure labels. Process status, failed controls, clock/web correlations
and missing output cannot establish the cause; the semantic judges decide or
abstain. Triage does not rewrite trial quality verdicts.

## Validation limits

No held-out human-labelled validation set has established the judges'
model-quality performance. Offline tests use canned responses to verify schemas,
abstention, evidence and process handling. They make no broad model-quality
claims. Run held-out calibration before publishing rankings.
