"""L1 tests: Decision gold states — EVALUATION.md §6.5, ROADMAP Phase 6.

Pins the Phase 6 acceptance: each rule's hit/miss is explainable; L0
prerequisites are LEARN_PREREQUISITE not REVIEW; QUIET never over-interrupts;
the user may choose to continue directly.
"""

from __future__ import annotations

from bookmind.domain.enums import ActivityMode
from bookmind.evaluation.decision_gold import (
    GOLD_CASES,
    assert_all_pass,
    run_decision_gold,
)


def test_decision_gold_all_cases_pass():
    assert_all_pass()


def test_decision_gold_has_ten_cases():
    # We expect the 10 hand-annotated cases that cover the Phase 6 criteria.
    assert len(GOLD_CASES) >= 10


def test_decision_gold_no_forbidden_action_selected():
    results = run_decision_gold()
    for r in results:
        assert r.passed, f"{r.case_id} failed: {r.failures}"


def test_quiet_never_proactively_interrupts():
    """Across every QUIET-READING/QUIET-REVIEW case, the system must not
    proactively select VERIFY/REVIEW/REMEDIATE/DIAGNOSE — only WAIT or
    CONTINUE_READING (the latter requires an active reading passage).

    ASSESSMENT is excluded: it is forced QUIET but VERIFY *is* the assessment
    task, not an interruption (LEARNING_MODEL §10)."""
    results = run_decision_gold()
    for gc, r in zip(GOLD_CASES, results):
        if gc.intervention_policy.value != "QUIET":
            continue
        if gc.activity_mode == ActivityMode.ASSESSMENT:
            continue  # VERIFY is the task, not an interruption
        if gc.user_requested_action is not None:
            continue  # user-initiated, not system-proactive
        assert r.selected_action in ("WAIT", "CONTINUE_READING"), (
            f"{gc.case_id}: QUIET proactively selected {r.selected_action}"
        )


def test_l0_prereq_case_uses_learn_not_review():
    """The L0-prerequisite gold case must fire LEARN_PREREQUISITE, never REVIEW."""
    results = run_decision_gold()
    for gc, r in zip(GOLD_CASES, results):
        if gc.case_id == "l0_prereq_is_learn_not_review":
            assert r.selected_action == "LEARN_PREREQUISITE"
            assert r.selected_rule == 7


def test_every_case_fires_expected_rule():
    """能解释每条规则是否命中: the firing rule must match the annotation."""
    results = run_decision_gold()
    for gc, r in zip(GOLD_CASES, results):
        assert r.selected_rule == gc.expected_rule, (
            f"{gc.case_id}: rule {r.selected_rule} != expected {gc.expected_rule}"
        )
