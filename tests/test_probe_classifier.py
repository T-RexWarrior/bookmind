"""Phase 5 tests: probe answer classifier — LEARNING_MODEL.md §8/§9.

Covers the "高区分度探针能区分核心假设" acceptance: a probe answer maps to
exactly one competing hypothesis; non-matching answers return None. Pure and
deterministic.
"""

from __future__ import annotations

from bookmind.agents.bug_library import BUG_REF_VS_OBJECT, BUG_EQ_VS_EQUALS, get_bug
from bookmind.engine.misconception.probe_classifier import (
    ClassificationResult,
    classify_answer,
    hypothesis_key,
    signals_for_probe,
)


def test_classify_matches_value_semantics_hypothesis():
    """The 'b is a copy / original value' answer matches h_value_semantics."""
    bug = BUG_REF_VS_OBJECT
    res = classify_answer(bug, "a.getValue() returns the original value because b is a separate copy.")
    assert res.best_hypothesis == "h_value_semantics"


def test_classify_matches_ref_aliasing_hypothesis():
    """The 'they share memory' answer matches h_ref_aliasing."""
    bug = BUG_REF_VS_OBJECT
    res = classify_answer(bug, "It's 9 because they share memory, both point to the same object.")
    assert res.best_hypothesis == "h_ref_aliasing"


def test_classify_returns_none_when_no_match():
    """An off-topic answer matches no hypothesis."""
    bug = BUG_REF_VS_OBJECT
    res = classify_answer(bug, "I like turtles.")
    assert res.best_hypothesis is None
    assert res.scores  # still populated
    assert all(s == 0 for s in res.scores.values())


def test_classify_distinguishes_eq_vs_equals_bugs():
    """The ==/equals bug's content-equality answer matches h_eq_is_content."""
    bug = BUG_EQ_VS_EQUALS
    res = classify_answer(bug, "== compares the contents of two Strings, so it returns true.")
    assert res.best_hypothesis == "h_eq_is_content"


def test_hypothesis_key_extracts_prefix():
    assert hypothesis_key("h_ref_aliasing: learner does not understand") == "h_ref_aliasing"
    assert hypothesis_key("h_value_semantics: ...") == "h_value_semantics"


def test_signals_for_probe_one_for_only():
    """A matching probe emits a single FOR signal for the bug.

    The competing hypotheses in a BugEntry share one bug_id (they are
    sub-explanations of one observable bug), so an AGAINST on the same bug_id
    would be scored as disproof of the very bug the probe supports. Mutual
    exclusion across *different* bug_ids is handled by mutual_exclusion.
    """
    bug = BUG_REF_VS_OBJECT
    signals = signals_for_probe(bug, "a.getValue() returns the original value because b is a separate copy.")
    assert len(signals) == 1
    assert signals[0].direction.value == "FOR"
    assert signals[0].bug_id == bug.bug_id
    assert "h_value_semantics" in signals[0].reason


def test_signals_for_probe_empty_when_no_match():
    """A non-matching answer produces no misconception signals (NEEDS_REVIEW)."""
    bug = BUG_REF_VS_OBJECT
    signals = signals_for_probe(bug, "I don't know.")
    assert signals == []


def test_classify_is_deterministic():
    bug = get_bug("bug_ref_vs_object")
    answer = "b is a copy so a keeps the original value"
    r1 = classify_answer(bug, answer)
    r2 = classify_answer(bug, answer)
    assert r1.best_hypothesis == r2.best_hypothesis
    assert r1.scores == r2.scores


def test_all_five_bugs_have_classifiable_wrong_answers():
    """Each core bug's first likely_wrong_answer must classify to a hypothesis."""
    from bookmind.agents.bug_library import BUG_LIBRARY
    for bug in BUG_LIBRARY.values():
        if not bug.likely_wrong_answers:
            continue
        res = classify_answer(bug, bug.likely_wrong_answers[0])
        assert res.best_hypothesis is not None, f"{bug.bug_id} wrong answer did not classify"
