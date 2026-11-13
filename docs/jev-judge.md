# Jev judging and attribution

Parallax grades semantic quality only through **Jev with an LLM fallback**.
There is no deterministic or heuristic grader. Execution validity is judged
separately from agent quality.

## Grading

`parallax.semantic` sends the user task, scheduled turns, world state and call
results, captured answers/finals, trusted verify output and file snapshots.
Existing check YAML stays readable as requirements; regexes, keywords, lengths
and counts are never executed to assign a quality verdict. Prefer explicit
rubric questions for new tasks.

Jev returns a probability for each requirement. Values strictly above 0.9 or
strictly below 0.1 settle it; a value at a threshold is uncertain. Uncertain,
failed or unavailable Jev calls use the configured LLM panel. Jev runs through
OpenRouter's System One route (`/api/v1/systemone`). A state that cannot fit
the input that route documents (262,144 tokens for the state plus every
question) is not sent, and a policy may route a class of tasks, such as browser
safety pauses, to the panel without asking Jev. OpenRouter runs a large state in
TypeSafe's long-context mode on its own, so Parallax sends no long-context
header. A question Jev refuses goes to the panel. An error reply from OpenRouter
is a known $0 attempt, as its zero completion insurance bills none; a timeout
stays unpriced. The fallback has the same guard: a request whose
estimated prompt cannot fit the pinned model's documented window is never sent,
and an upstream "prompt is too long" refusal is a known $0. Either leaves the
question unknown with that reason; evidence is never truncated to fit. Images
in the evidence (`data:image/...` strings, such as a hosted session's
screenshots) reach the fallback as image content parts, each distinct image
once, referenced from the JSON by `image:sha256:<hex>`, and are sized by the
provider's visual-token rule. Jev reads text only, so evidence with images goes
to the fallback. An image set over a documented limit is not sent, never
downscaled or dropped, and the reason is recorded. The
fallback requires agreement from every panel model and permits abstention.
Missing configuration, disagreement or unavailable evidence leaves the check
ungraded and the trial INVALID. No skipped judge can create a pass.

Evidence is never silently truncated to obtain a score. The current grading
budget is 256 KiB; oversized evidence, binary files and symlink snapshots need
a supported evidence representation before grading. This is an execution limit,
not a judgement about the agent. Same-family fallback use remains visible.

The policy version and Jev model and endpoint are part of resume identity and
of each run's recorded judge policy, so `parallax compare` never pairs runs
graded by different evaluators unless `--allow-identity-change` is given.

## Configuration

- Jev: `OPENROUTER_API_KEY`; optional `PARALLAX_JEV_MODEL` and
  `PARALLAX_JEV_BASE_URL` (defaults are declared in `parallax/jev.py`).
- LLM fallback: `--judge` or `PARALLAX_JUDGE_MODELS`; with neither, `DEFAULT_MODEL` in
  `parallax/judge.py` (DeepSeek V4.1 Flash 1017) once a key can reach it.
  `PARALLAX_JUDGE_API_KEY` defaults to `OPENROUTER_API_KEY` and
  `PARALLAX_JUDGE_BASE_URL` to OpenRouter. With no key there is no fallback,
  so checks Jev is unsure about stay INVALID and nothing is spent.
- `parallax check` uses the same judges for oracle, null controls and mutants.
  It makes model calls. `parallax check --lint-only` and CI protocol tests run
  offline.

Only recorded world internal errors and boot failures receive direct
infrastructure labels. Process status, failed controls, clock/web correlations
and missing output cannot establish the cause; the semantic judges decide or
abstain. Triage does not rewrite trial quality verdicts.

## Validation limits

No held-out human-labelled validation set has established the judges'
model-quality performance. Offline tests use canned responses to verify schemas, fallback,
abstention, evidence and process handling. They make no broad model-quality
claims. Run held-out calibration before publishing rankings.
