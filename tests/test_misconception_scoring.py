"""L1 tests: misconception fixed-evidence scoring — LEARNING_MODEL.md §8.

Each Evidence contributes exactly one scoring bucket. Replaying must not
double-count. Confidence bands are discrete display labels, not probabilities.
"""

from __future__ import annotations

from bookmind.domain.enums import (
    ConfidenceBand,
    EvidenceResult,
    EvidenceType,
    SignalDirection,
    SignalStrength,
)
from bookmind.domain.models import Evidence, MisconceptionSignal
from bookmind.engine.misconception.scoring import (
    confidence_band,
    is_high_discrimination_support,
    score_for,
    scoring_type,
)


def _ev(
    *,
    evidence_type=EvidenceType.VERIFY,
    result=EvidenceResult.PASS,
    signals=None,
    high_disc=False,
    independent=True,
) -> Evidence:
    return Evidence(
        evidence_id="e1",
        event_key="k1",
        project_id="p",
        concept_id="c",
        source_book_id="b",
        evidence_type=evidence_type,
        required_level="L1",
        result=result,
        independent=independent,
        misconception_signals=signals or [],
        high_discrimination=high_disc,
    )


def test_weak_support_scores_one():
    e = _ev(signals=[MisconceptionSignal(bug_id="bug", direction=SignalDirection.FOR, strength=SignalStrength.WEAK)])
    assert scoring_type(e) == "support"
    assert score_for(e) == 1


def test_medium_support_scores_two():
    e = _ev(signals=[MisconceptionSignal(bug_id="bug", direction=SignalDirection.FOR, strength=SignalStrength.MEDIUM)])
    assert score_for(e) == 2


def test_strong_support_scores_three():
    e = _ev(signals=[MisconceptionSignal(bug_id="bug", direction=SignalDirection.FOR, strength=SignalStrength.STRONG)])
    assert score_for(e) == 3


def test_high_discrimination_probe_scores_three_not_stacked():
    e = _ev(
        evidence_type=EvidenceType.PROBE,
        high_disc=True,
        signals=[MisconceptionSignal(bug_id="bug", direction=SignalDirection.FOR, strength=SignalStrength.STRONG)],
    )
    # High-disc probe is +3; it must NOT also add the STRONG +3 (no double count).
    assert scoring_type(e) == "probe_high_disc"
    assert score_for(e) == 3


def test_explicit_disproof_scores_minus_two():
    e = _ev(signals=[MisconceptionSignal(bug_id="bug", direction=SignalDirection.AGAINST, strength=SignalStrength.STRONG)])
    assert scoring_type(e) == "explicit_disproof"
    assert score_for(e) == -2


def test_changed_task_independent_pass_scores_minus_two():
    e = _ev(evidence_type=EvidenceType.CHANGED_TASK, result=EvidenceResult.PASS, independent=True)
    assert scoring_type(e) == "changed_task_pass"
    assert score_for(e) == -2


def test_changed_task_pass_does_not_also_count_as_disproof():
    """changed-task PASS already encodes disproof; no extra -2 stacking."""
    e = _ev(
        evidence_type=EvidenceType.CHANGED_TASK,
        result=EvidenceResult.PASS,
        independent=True,
        signals=[MisconceptionSignal(bug_id="bug", direction=SignalDirection.AGAINST, strength=SignalStrength.STRONG)],
    )
    assert scoring_type(e) == "changed_task_pass"
    assert score_for(e) == -2  # not -4


def test_no_signal_scores_zero():
    e = _ev()
    assert scoring_type(e) == "none"
    assert score_for(e) == 0


def test_replay_does_not_double_count():
    """Summing the same evidence list twice must use each evidence once.

    The Engine keys contributions by evidence_id; here we verify the score
    function is a pure per-evidence map (the dedup is the caller's job, but
    the per-evidence value is stable).
    """
    e = _ev(signals=[MisconceptionSignal(bug_id="bug", direction=SignalDirection.FOR, strength=SignalStrength.MEDIUM)])
    assert score_for(e) == 2
    assert score_for(e) == 2  # same evidence, same value


def test_confidence_bands():
    assert confidence_band(0) == ConfidenceBand.LOW
    assert confidence_band(3) == ConfidenceBand.LOW
    assert confidence_band(4) == ConfidenceBand.MEDIUM
    assert confidence_band(5) == ConfidenceBand.MEDIUM
    assert confidence_band(6) == ConfidenceBand.HIGH
    assert confidence_band(20) == ConfidenceBand.HIGH


def test_is_high_discrimination_support():
    e = _ev(evidence_type=EvidenceType.PROBE, high_disc=True, signals=[MisconceptionSignal(bug_id="bug", direction=SignalDirection.FOR, strength=SignalStrength.STRONG)])
    assert is_high_discrimination_support(e) is True
    e2 = _ev(evidence_type=EvidenceType.PROBE, signals=[MisconceptionSignal(bug_id="bug", direction=SignalDirection.FOR, strength=SignalStrength.STRONG)])
    assert is_high_discrimination_support(e2) is False
