"""L1 tests: misconception state machine — LEARNING_MODEL.md §8.

Covers the full lifecycle: SUSPECTED → LIKELY → CONFIRMED → REMEDIATING →
VERIFYING → RESOLVED, plus DISMISSED, RELAPSED and the failure/rollback paths.
Also pins: one error does not confirm a bug; two distinct scenarios required
for RESOLVED; mutually-exclusive hypotheses cannot both CONFIRMED (handled at
the decision layer, here we test single-hypothesis transitions).
"""

from __future__ import annotations

from bookmind.domain.enums import (
    EvidenceResult,
    EvidenceType,
    MisconceptionStatus,
    SignalDirection,
    SignalStrength,
)
from bookmind.domain.models import Evidence, MisconceptionHypothesis, MisconceptionSignal
from bookmind.engine.misconception.state_machine import update


def _hyp(status=MisconceptionStatus.SUSPECTED, score=0) -> MisconceptionHypothesis:
    return MisconceptionHypothesis(
        project_id="p", bug_id="bug1", evidence_score=score, status=status
    )


def _ev(
    eid,
    *,
    evidence_type=EvidenceType.VERIFY,
    result=EvidenceResult.PASS,
    direction=SignalDirection.FOR,
    strength=SignalStrength.MEDIUM,
    task_id="t",
    high_disc=False,
    fingerprint=None,
    independent=True,
) -> Evidence:
    signals = []
    if direction is not None:
        signals.append(MisconceptionSignal(bug_id="bug1", direction=direction, strength=strength))
    return Evidence(
        evidence_id=eid,
        event_key=f"k_{eid}",
        project_id="p",
        concept_id="c",
        source_book_id="b",
        evidence_type=evidence_type,
        required_level="L1",
        result=result,
        independent=independent,
        task_id=task_id,
        misconception_signals=signals,
        high_discrimination=high_disc,
        scenario_fingerprint=fingerprint,
    )


# --- Entry thresholds ----------------------------------------------------

def test_single_weak_signal_suspected():
    e = [_ev("e1", strength=SignalStrength.WEAK)]  # +1 → score 1 < 2 → still SUSPECTED floor
    # Actually +1 < 2 so stays SUSPECTED (floor). Confirm:
    r = update(_hyp(), e)
    assert r.new_status == MisconceptionStatus.SUSPECTED


def test_two_medium_signals_likely():
    e = [_ev("e1", strength=SignalStrength.MEDIUM), _ev("e2", strength=SignalStrength.MEDIUM, task_id="t2")]
    # +2 +2 = 4 → LIKELY
    r = update(_hyp(), e)
    assert r.new_status == MisconceptionStatus.LIKELY


def test_one_error_does_not_confirm():
    """A single STRONG support (+3) must NOT reach CONFIRMED (needs >=6)."""
    e = [_ev("e1", strength=SignalStrength.STRONG)]
    r = update(_hyp(), e)
    assert r.new_status != MisconceptionStatus.CONFIRMED


def test_confirmed_requires_score_two_tasks_and_probe():
    e = [
        _ev("e1", strength=SignalStrength.STRONG, task_id="t1"),  # +3
        _ev("e2", strength=SignalStrength.STRONG, task_id="t2"),  # +3
        _ev("e3", evidence_type=EvidenceType.PROBE, high_disc=True, task_id="t3"),  # +3 (high-disc probe)
    ]
    r = update(_hyp(), e)
    assert r.new_status == MisconceptionStatus.CONFIRMED
    assert r.hypothesis.evidence_score == 9


def test_confirmed_requires_probe():
    """Score>=6 and 2 tasks but no probe → NOT confirmed."""
    e = [
        _ev("e1", strength=SignalStrength.STRONG, task_id="t1"),
        _ev("e2", strength=SignalStrength.STRONG, task_id="t2"),
    ]
    r = update(_hyp(), e)
    assert r.new_status != MisconceptionStatus.CONFIRMED


def test_confirmed_requires_two_distinct_tasks():
    """Score>=6 with probe but all from one task → NOT confirmed."""
    e = [
        _ev("e1", strength=SignalStrength.STRONG, task_id="t1"),
        _ev("e2", strength=SignalStrength.STRONG, task_id="t1"),
        _ev("e3", evidence_type=EvidenceType.PROBE, high_disc=True, task_id="t1"),
    ]
    r = update(_hyp(), e)
    assert r.new_status != MisconceptionStatus.CONFIRMED


# --- DISMISSED -----------------------------------------------------------

def test_dismissed_when_disproof_and_low_score():
    e = [
        _ev("e1", strength=SignalStrength.WEAK),  # +1
        _ev("e2", direction=SignalDirection.AGAINST, strength=SignalStrength.STRONG, task_id="t2"),  # -2 → -1
    ]
    r = update(_hyp(), e)
    assert r.new_status == MisconceptionStatus.DISMISSED


def test_dismissed_can_reactivate():
    e = [_ev("e1", direction=SignalDirection.AGAINST)]  # -2 → dismissed
    r = update(_hyp(), e)
    assert r.new_status == MisconceptionStatus.DISMISSED
    # new support brings score back >= 2
    e2 = e + [_ev("e2", strength=SignalStrength.MEDIUM, task_id="t2"), _ev("e3", strength=SignalStrength.MEDIUM, task_id="t3")]
    r2 = update(r.hypothesis, e2)
    assert r2.new_status == MisconceptionStatus.SUSPECTED
    assert r2.hypothesis.hypothesis_cycle == 1


# --- Remediation lifecycle -----------------------------------------------

def test_confirmed_to_remediating():
    base_e = [
        _ev("e1", strength=SignalStrength.STRONG, task_id="t1"),
        _ev("e2", strength=SignalStrength.STRONG, task_id="t2"),
        _ev("e3", evidence_type=EvidenceType.PROBE, high_disc=True, task_id="t3"),
    ]
    r = update(_hyp(), base_e)
    assert r.new_status == MisconceptionStatus.CONFIRMED
    r2 = update(r.hypothesis, base_e, remediation_started=True)
    assert r2.new_status == MisconceptionStatus.REMEDIATING
    assert r2.hypothesis.remediation_version == 1


def test_first_changed_task_pass_to_verifying():
    base_e = [
        _ev("e1", strength=SignalStrength.STRONG, task_id="t1"),
        _ev("e2", strength=SignalStrength.STRONG, task_id="t2"),
        _ev("e3", evidence_type=EvidenceType.PROBE, high_disc=True, task_id="t3"),
    ]
    h = update(_hyp(), base_e).hypothesis
    h = update(h, base_e, remediation_started=True).hypothesis
    # first changed-task PASS — submit ONLY the new task as new_evidence
    ct = _ev("ct1", evidence_type=EvidenceType.CHANGED_TASK, result=EvidenceResult.PASS, fingerprint="scene_A", task_id="ct")
    r = update(h, base_e + [ct], new_evidence=[ct])
    assert r.new_status == MisconceptionStatus.VERIFYING
    assert r.hypothesis.changed_task_pass_count == 1


def test_two_distinct_scenarios_to_resolved():
    base_e = [
        _ev("e1", strength=SignalStrength.STRONG, task_id="t1"),
        _ev("e2", strength=SignalStrength.STRONG, task_id="t2"),
        _ev("e3", evidence_type=EvidenceType.PROBE, high_disc=True, task_id="t3"),
    ]
    h = update(_hyp(), base_e).hypothesis
    h = update(h, base_e, remediation_started=True).hypothesis
    ct1 = _ev("ct1", evidence_type=EvidenceType.CHANGED_TASK, result=EvidenceResult.PASS, fingerprint="scene_A", task_id="ct1")
    h = update(h, base_e + [ct1], new_evidence=[ct1]).hypothesis
    ct2 = _ev("ct2", evidence_type=EvidenceType.CHANGED_TASK, result=EvidenceResult.PASS, fingerprint="scene_B", task_id="ct2")
    r = update(h, base_e + [ct1, ct2], new_evidence=[ct2])
    assert r.new_status == MisconceptionStatus.RESOLVED
    assert r.hypothesis.changed_task_pass_count == 2


def test_duplicate_scenario_does_not_resolve():
    """Two PASS with the SAME fingerprint must NOT reach RESOLVED."""
    base_e = [
        _ev("e1", strength=SignalStrength.STRONG, task_id="t1"),
        _ev("e2", strength=SignalStrength.STRONG, task_id="t2"),
        _ev("e3", evidence_type=EvidenceType.PROBE, high_disc=True, task_id="t3"),
    ]
    h = update(_hyp(), base_e).hypothesis
    h = update(h, base_e, remediation_started=True).hypothesis
    ct1 = _ev("ct1", evidence_type=EvidenceType.CHANGED_TASK, result=EvidenceResult.PASS, fingerprint="scene_A", task_id="ct1")
    h = update(h, base_e + [ct1], new_evidence=[ct1]).hypothesis
    ct2 = _ev("ct2", evidence_type=EvidenceType.CHANGED_TASK, result=EvidenceResult.PASS, fingerprint="scene_A", task_id="ct2")  # same
    r = update(h, base_e + [ct1, ct2], new_evidence=[ct2])
    assert r.new_status == MisconceptionStatus.VERIFYING
    assert r.hypothesis.changed_task_pass_count == 1


def test_changed_task_fail_back_to_confirmed():
    base_e = [
        _ev("e1", strength=SignalStrength.STRONG, task_id="t1"),
        _ev("e2", strength=SignalStrength.STRONG, task_id="t2"),
        _ev("e3", evidence_type=EvidenceType.PROBE, high_disc=True, task_id="t3"),
    ]
    h = update(_hyp(), base_e).hypothesis
    h = update(h, base_e, remediation_started=True).hypothesis
    ct_fail = _ev("ct1", evidence_type=EvidenceType.CHANGED_TASK, result=EvidenceResult.FAIL, fingerprint="scene_A", task_id="ct1")
    r = update(h, base_e + [ct_fail], new_evidence=[ct_fail])
    assert r.new_status == MisconceptionStatus.CONFIRMED
    assert r.hypothesis.changed_task_pass_count == 0


def test_resolved_relapses_on_new_high_disc_support():
    base_e = [
        _ev("e1", strength=SignalStrength.STRONG, task_id="t1"),
        _ev("e2", strength=SignalStrength.STRONG, task_id="t2"),
        _ev("e3", evidence_type=EvidenceType.PROBE, high_disc=True, task_id="t3"),
    ]
    h = update(_hyp(), base_e).hypothesis
    h = update(h, base_e, remediation_started=True).hypothesis
    ct1 = _ev("ct1", evidence_type=EvidenceType.CHANGED_TASK, result=EvidenceResult.PASS, fingerprint="A", task_id="ct1")
    ct2 = _ev("ct2", evidence_type=EvidenceType.CHANGED_TASK, result=EvidenceResult.PASS, fingerprint="B", task_id="ct2")
    h = update(h, base_e + [ct1], new_evidence=[ct1]).hypothesis
    h = update(h, base_e + [ct1, ct2], new_evidence=[ct2]).hypothesis
    assert h.status == MisconceptionStatus.RESOLVED
    relapse = _ev("r1", evidence_type=EvidenceType.PROBE, high_disc=True, task_id="r1")
    r = update(h, base_e + [ct1, ct2, relapse], new_evidence=[relapse])
    assert r.new_status == MisconceptionStatus.RELAPSED


def test_historical_probe_does_not_reopen_remediation():
    """A previously-confirmed probe must not re-flip REMEDIATING→CONFIRMED
    when a changed task is later submitted (only *new* support re-opens)."""
    base_e = [
        _ev("e1", strength=SignalStrength.STRONG, task_id="t1"),
        _ev("e2", strength=SignalStrength.STRONG, task_id="t2"),
        _ev("e3", evidence_type=EvidenceType.PROBE, high_disc=True, task_id="t3"),
    ]
    h = update(_hyp(), base_e).hypothesis
    h = update(h, base_e, remediation_started=True).hypothesis
    assert h.status == MisconceptionStatus.REMEDIATING
    ct1 = _ev("ct1", evidence_type=EvidenceType.CHANGED_TASK, result=EvidenceResult.PASS, fingerprint="A", task_id="ct1")
    r = update(h, base_e + [ct1], new_evidence=[ct1])
    # Must advance to VERIFYING, NOT get pulled back to CONFIRMED by old probe.
    assert r.new_status == MisconceptionStatus.VERIFYING
