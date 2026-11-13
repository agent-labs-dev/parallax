"""Control replay and verdict propagation; offline fixtures make no quality claims."""

import shutil
from pathlib import Path

import pytest
from parallax.controls import check_async, mutate_approvals, outcome
from parallax.models import (
    ApprovalAnswer,
    ApprovalDecision,
    CheckResult,
    Isolation,
    Mutation,
    MutationExpect,
    Verdict,
)
from parallax.task import discover, load_task

TASKS = Path(__file__).parent / "fixtures" / "tasks"


async def test_unjudged_controls_cannot_certify_tasks() -> None:
    report = await check_async(discover([TASKS]), Isolation.HOST)
    assert not report.ok
    assert all(t.oracle is Verdict.INVALID for t in report.tasks)
    assert all(not t.mutants for t in report.tasks)


@pytest.mark.parametrize(
    ("expect", "passed", "ok"),
    [
        (MutationExpect.FAIL, False, True),
        (MutationExpect.FAIL, True, False),
        (MutationExpect.PASS, True, True),
        (MutationExpect.PASS, False, False),
        (MutationExpect.FAIL, None, False),
        (MutationExpect.PASS, None, False),
    ],
)
def test_control_outcome_propagates_the_judges_decision(expect: MutationExpect, passed: bool | None, ok: bool) -> None:
    mutation = Mutation(id="m", note="", expect=expect, must_fail=("c",) if expect is MutationExpect.FAIL else ())
    result = CheckResult(
        id="c",
        kind="rubric",
        mode="wrong_output",
        gate=True,
        advisory=False,
        weight=1,
        passed=passed,
        detail="canned judge reply",
    )
    assert outcome(mutation, [result]).ok is ok


def test_approval_mutants_replace_a_turns_cards_with_the_steps_decision(tmp_path: Path) -> None:
    root = tmp_path / "mini-steps"
    shutil.copytree(TASKS / "mini-steps", root)
    toml = root / "task.toml"
    toml.write_text(
        toml.read_text()
        .replace('["mcp", "multi_turn"]', '["mcp", "multi_turn", "unattended"]')
        .replace('patch = "sam-joins"', 'patch = "sam-joins"\nsession = "new"\norigin = "job"\napproval = "deny"')
    )
    task = load_task(root)
    oracle = (ApprovalAnswer(turn=2, request="run report.sh", decision=ApprovalDecision.DENY),)
    retried = Mutation(id="retry", note="", must_fail=("c",), approvals={2: ("run report.sh", "run report.sh again")})
    assert [a.request for a in mutate_approvals(task, oracle, retried)] == ["run report.sh", "run report.sh again"]
    assert all(a.decision is ApprovalDecision.DENY for a in mutate_approvals(task, oracle, retried))
    never_asked = Mutation(id="none", note="", must_fail=("c",), base="seed")
    assert mutate_approvals(task, oracle, never_asked) == ()
    with pytest.raises(ValueError, match="scripts no approval"):
        mutate_approvals(task, oracle, Mutation(id="bad", note="", must_fail=("c",), approvals={1: ("x",)}))
