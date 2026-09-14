"""L3 tests: GoldenCase replay — EVALUATION.md §4.1 offline fixed trajectory.

Each case asserts the decision engine selects the expected action, never a
forbidden one. These are the CI gate fixtures (EVALUATION.md §8 item 2).
"""

from __future__ import annotations

from bookmind.evaluation.golden_cases import GOLDEN_CASES
from bookmind.evaluation.replay import run_all, run_case


def test_all_golden_cases_pass():
    results = run_all()
    failures = [r for r in results if not r.passed]
    assert not failures, (
        "Golden case failures:\n"
        + "\n".join(f"  {r.case_id}: {r.failures}" for r in failures)
    )
    assert len(results) == 10


def test_each_case_individually():
    for case in GOLDEN_CASES:
        r = run_case(case)
        assert r.passed, f"{case.case_id}: expected {r.expected_action}, got {r.selected_action} — {r.failures}"


def test_assessment_case_never_answer_or_remediate():
    r = run_case(next(c for c in GOLDEN_CASES if c.case_id == "gc_08_assessment_only_verify"))
    assert r.passed
    assert r.selected_action == "VERIFY"


def test_confirmed_misconception_remediates_not_verifies():
    r = run_case(next(c for c in GOLDEN_CASES if c.case_id == "gc_05_confirmed_misconception_remediate"))
    assert r.selected_action == "REMEDIATE"


def test_quiet_reading_does_not_verify():
    r = run_case(next(c for c in GOLDEN_CASES if c.case_id == "gc_01_quiet_reading_waits"))
    assert r.selected_action in ("CONTINUE_READING", "WAIT")
    assert r.selected_action != "VERIFY"
