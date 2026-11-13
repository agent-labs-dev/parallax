---
name: task-authoring
description: Write or change a Parallax benchmark task (tasks/core/<id>/ or review/drafts/). Use when adding a task, editing checks, seeds, mutants or oracles, or when `parallax check` flags a task as broken.
---

# Task authoring

The canonical reference is [docs/authoring.md](../../../docs/authoring.md). Read it before you edit a task. The rules below are the ones most often broken:

1. **Grade world-owned evidence only.** That means final state, the complete served tool-schema catalogue, the authored fault schedule, the call log, the workspace, a verify command or answers. Schemas establish capabilities available to the agent; calls establish attempts, and final state establishes effects. Preserve fault tool names and call ranges so the judge can distinguish an operation-local failure from a service outage. Tool diagnostics explain failures but never prove effects. Collect final state only after the product reports the Task settled, including delegations.
2. **Only Jev + LLM fallback grade semantic quality.** Every task needs a semantic gate; prefer rubric questions about observable work. Never add regex, keyword, length or count-based quality gates. Legacy declarations are judge inputs only. Execution validity is separate: exact boot failures stay INVALID; Jev with LLM fallback attributes ambiguous interruption, infrastructure failure or contamination before quality is judged. Agent-caused failures remain eligible for FAIL.
3. **Semantic controls must hold before claiming benchmark validity.** Configure the judges; offline protocol tests cannot certify quality. The oracle passes, `null-claim` and `null-refuse` fail, every mutant is killed and every `expect: pass` mutant (a different correct solution) passes every gate. If the oracle writes, at least one mutant is `collateral: true`. When a check pins a detail the instruction leaves open, add an `expect: pass` mutant to prove it is not too strict.
4. **Negative twin.** A behaviour rewarded here, such as acting, asking or sending, must be wrong in some other task. Every restraint task has a positive answer check.
5. **Pick the failure mode from the agent's mistake**, not from the check kind ([docs/taxonomy.md](../../../docs/taxonomy.md)).
6. **Use synthetic data only.** Use invented people and `.test`/`.example` domains. Generate a fresh uuid4 canary and put it in a trailing comment of the first instruction.
7. **Bump `version`** whenever a change alters grading. Version semantic judging policies independently and retain earlier judgments. Grounding includes the scope and conditions of claims, not just their numbers; qualitative rankings, superlatives and causal claims need source support just as figures do, and blanket claims that nothing was added must be checked against the delivered artifact and answer. Validate rubric changes against both unsupported claims and faithful paraphrases with the model judge. A conditional capability offer can be supported by a callable schema and a resolved target without a test mutation; do not invent restrictions absent from the evidence. Capability alone does not prove success or autonomous follow-up. Claims about resource contents or absence require user/source material or observed call results; hidden world state proves effects, not that an agent inspected them.
8. **Dates come from the world clock.** Seed `now` is the user's present. Agents read it with `user_clock_now` (under `docker` isolation the box's wall clock follows the world too; under `host` it does not). Never write a check that only passes if the agent trusts the machine's clock.
9. **Grade timing with turn-scoped files.** When an edit is only wrong if it happens too early, use a file check with `turn: N` (the snapshot after turn N) and a `turn_files` mutant that makes the early edit.
10. **Sent mail is readable.** `mail_list_sent` lets an agent confirm a send landed; grade the `mail.sent` rows, not the claim.
11. **Judge usage is append-only.** Preserve every Jev and fallback attempt's provider-reported tokens, charge when supplied, latency, and errors; never estimate a missing charge or overwrite an earlier retry.

Validate structure offline, then run semantic controls with configured judges (paid calls require authorization):

```sh
uv run parallax check <task-dir> --lint-only
uv run parallax check <task-dir> --isolation host
```

When a check fails, fix the task, not the controls. Never weaken a check just to make the oracle pass, and never loosen a mutant just to make it die.

