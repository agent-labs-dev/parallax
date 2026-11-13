# Judge calibration

Semantic grading uses Jev with an LLM fallback for every check.

Before publishing rankings, collect human-labelled cases covering correct work,
plausible failures, equivalent wording, negation, ambiguous requirements and
infrastructure failures. Keep tuning and held-out sets separate. Record the
exact policy, judge models/endpoints, task versions and evidence representation;
report agreement, false positives, coverage/abstention and per-failure-mode results.

No held-out quality calibration has been run for the current policy. Offline CI
tests validate protocol behavior only. See [judging](../docs/jev-judge.md).
