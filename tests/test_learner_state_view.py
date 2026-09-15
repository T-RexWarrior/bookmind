"""L1 tests: Learner State view — LEARNING_MODEL §3/§13, ARCHITECTURE §12.

Pins the display projection: derived effective status, retrievability refresh,
evidence chain ordering, and the verified/pending/weak/due grouping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bookmind.domain.enums import (
    EvidenceResult,
    EvidenceType,
    ExposureState,
    Level,
    LevelStatus,
)
from bookmind.domain.models import (
    Concept,
    Evidence,
    LearnerConceptState,
    LevelRecord,
    ReviewPolicy,
)
from bookmind.services.learner_state_view import (
    GROUP_DUE,
    GROUP_PENDING,
    GROUP_VERIFIED,
    GROUP_WEAK,
    build_state_view,
)


def _concept(cid="c1") -> Concept:
    return Concept(concept_id=cid, book_id="b", name=cid, importance=0.8, goal_relevance=0.8)


def _state(cid="c1") -> LearnerConceptState:
    return LearnerConceptState(project_id="p", concept_id=cid)


def _policy() -> ReviewPolicy:
    return ReviewPolicy()


def _ev(eid, etype=EvidenceType.VERIFY, result=EvidenceResult.PASS, when=None) -> Evidence:
    return Evidence(
        evidence_id=eid, event_key=f"k{eid}", project_id="p", concept_id="c1",
        source_book_id="b", evidence_type=etype, required_level=Level.L1,
        result=result, independent=True, occurred_at=when or datetime(2025, 1, 1, tzinfo=timezone.utc),
        task_id=f"t{eid}",
    )


# --- derived effective status on the view ---------------------------------

def test_view_shows_derived_blocked_when_lower_lapsed():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    s.levels[Level.L1.value] = LevelRecord(status=LevelStatus.VERIFIED, verified_at=t0, stability_days=2.0)
    s.levels[Level.L2.value] = LevelRecord(status=LevelStatus.VERIFIED, verified_at=t0, stability_days=4.0)
    as_of = t0 + timedelta(days=3)  # L1 expired (S=2), L2 raw-VERIFIED
    v = build_state_view(_concept(), s, [], as_of=as_of, policy=_policy())
    l2 = next(lv for lv in v.levels if lv.level == "L2")
    assert l2.raw_status == "VERIFIED"
    assert l2.effective_status == "BLOCKED_BY_LOWER_LEVEL"


def test_view_refreshes_retrievability_on_read():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    s.levels[Level.L1.value] = LevelRecord(status=LevelStatus.VERIFIED, verified_at=t0, stability_days=4.0)
    as_of = t0 + timedelta(days=4)  # R ≈ 0.9
    v = build_state_view(_concept(), s, [], as_of=as_of, policy=_policy())
    l1 = v.levels[0]
    assert 0.85 < l1.retrievability <= 0.91


# --- evidence chain -------------------------------------------------------

def test_evidence_ordered_newest_first():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    ev = [
        _ev("e1", when=t0),
        _ev("e2", when=t0 + timedelta(days=2)),
        _ev("e3", when=t0 + timedelta(days=1)),
    ]
    v = build_state_view(_concept(), s, ev, as_of=t0, policy=_policy())
    assert [e.evidence_id for e in v.evidence] == ["e2", "e3", "e1"]


def test_evidence_view_carries_trusted_fields():
    s = _state()
    ev = [_ev("e1", result=EvidenceResult.PARTIAL)]
    v = build_state_view(_concept(), s, ev, as_of=datetime(2025, 1, 1, tzinfo=timezone.utc), policy=_policy())
    assert v.evidence[0].result == "PARTIAL"
    assert v.evidence[0].independent is True
    assert v.evidence[0].required_level == "L1"


# --- grouping (ARCHITECTURE §12) ------------------------------------------

def test_group_verified_when_level_verified():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    s.levels[Level.L1.value] = LevelRecord(status=LevelStatus.VERIFIED, verified_at=t0, stability_days=4.0)
    v = build_state_view(_concept(), s, [], as_of=t0, policy=_policy())
    assert v.group == GROUP_VERIFIED


def test_group_due_when_expired():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    s.levels[Level.L1.value] = LevelRecord(status=LevelStatus.VERIFIED, verified_at=t0, stability_days=2.0)
    v = build_state_view(_concept(), s, [], as_of=t0 + timedelta(days=30), policy=_policy())
    assert v.group == GROUP_DUE


def test_group_weak_when_unstable():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    s.levels[Level.L1.value] = LevelRecord(status=LevelStatus.UNSTABLE, verified_at=t0, stability_days=2.0)
    v = build_state_view(_concept(), s, [], as_of=t0, policy=_policy())
    assert v.group == GROUP_WEAK


def test_group_pending_when_seen_but_unverified():
    s = _state()
    s.exposure_state = ExposureState.SEEN
    v = build_state_view(_concept(), s, [], as_of=datetime(2025, 1, 1, tzinfo=timezone.utc), policy=_policy())
    assert v.group == GROUP_PENDING


def test_group_weak_after_independent_l0_partial_attempt():
    """An attempted-but-not-yet-verifiable concept must not look untouched."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    v = build_state_view(
        _concept(), _state(), [_ev("p1", result=EvidenceResult.PARTIAL, when=t0)],
        as_of=t0, policy=_policy(),
    )
    assert v.current_verified_level == "L0"
    assert v.group == GROUP_WEAK


def test_group_due_when_review_due_in_past():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    s.levels[Level.L1.value] = LevelRecord(
        status=LevelStatus.VERIFIED, verified_at=t0, stability_days=100.0,
        review_due_at=t0 - timedelta(days=1),  # due in the past
    )
    v = build_state_view(_concept(), s, [], as_of=t0, policy=_policy())
    assert v.group == GROUP_DUE
