"""L1 tests: mastery state recomputation — LEARNING_MODEL.md §5 invariants.

Pins the highest/current separation and the contiguous-validity rule:
  - current_verified_level is the highest *continuously* VERIFIED level
  - a lapsed lower level blocks higher levels (derived BLOCKED_BY_LOWER_LEVEL)
    without deleting their history
  - highest_ever_level is monotonic (never decremented by disconfirmation)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bookmind.domain.enums import (
    DerivedEffectiveStatus,
    Level,
    LevelStatus,
)
from bookmind.domain.models import LearnerConceptState, LevelRecord, ReviewPolicy
from bookmind.engine.mastery.state import derived_effective_status, recompute


def _state() -> LearnerConceptState:
    return LearnerConceptState(project_id="p", concept_id="c")


def _policy() -> ReviewPolicy:
    return ReviewPolicy()


def test_fresh_state_is_l0():
    s = recompute(_state(), datetime(2025, 1, 1, tzinfo=timezone.utc), _policy())
    assert s.current_verified_level == Level.L0
    assert s.highest_ever_level == Level.L0


def test_verified_l1_rises_current_and_highest():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    s.levels[Level.L1.value] = LevelRecord(
        status=LevelStatus.VERIFIED, verified_at=t0, stability_days=2.0
    )
    out = recompute(s, t0, _policy())
    assert out.current_verified_level == Level.L1
    assert out.highest_ever_level == Level.L1


def test_contiguous_run_to_l3():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    for lvl in (Level.L1, Level.L2, Level.L3):
        s.levels[lvl.value] = LevelRecord(status=LevelStatus.VERIFIED, verified_at=t0, stability_days=5.0)
    out = recompute(s, t0, _policy())
    assert out.current_verified_level == Level.L3


def test_gap_blocks_higher_levels():
    """L1 unverified, L2 verified → current must be L0 (gap at L1)."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    s.levels[Level.L1.value] = LevelRecord(status=LevelStatus.UNVERIFIED)
    s.levels[Level.L2.value] = LevelRecord(status=LevelStatus.VERIFIED, verified_at=t0, stability_days=5.0)
    out = recompute(s, t0, _policy())
    assert out.current_verified_level == Level.L0


def test_lapsed_lower_level_blocks_higher_derived_status():
    """L1 expired, L2 verified → L2 derived status is BLOCKED_BY_LOWER_LEVEL."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    s.levels[Level.L1.value] = LevelRecord(
        status=LevelStatus.VERIFIED, verified_at=t0, stability_days=2.0
    )
    s.levels[Level.L2.value] = LevelRecord(
        status=LevelStatus.VERIFIED, verified_at=t0, stability_days=4.0
    )
    # Far enough that L1 (S=2) expires but L2 (S=4)... also expires at 30 days.
    # Use a moderate gap so only L1 expires: L1 expires when R<0.9 i.e. t>2d.
    as_of = t0 + timedelta(days=3)
    out = recompute(s, as_of, _policy())
    assert out.levels[Level.L1.value].status == LevelStatus.EXPIRED
    # L2 still raw-VERIFIED but derived BLOCKED.
    assert out.levels[Level.L2.value].status == LevelStatus.VERIFIED
    assert derived_effective_status(Level.L2, out) == DerivedEffectiveStatus.BLOCKED_BY_LOWER_LEVEL


def test_highest_ever_never_decreases():
    """If current drops to L0, highest_ever must retain its peak."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    s.highest_ever_level = Level.L3
    s.levels[Level.L1.value] = LevelRecord(status=LevelStatus.UNVERIFIED)
    out = recompute(s, t0, _policy())
    assert out.current_verified_level == Level.L0
    assert out.highest_ever_level == Level.L3  # unchanged


def test_expired_does_not_delete_history():
    """Expiry changes status but verified_at/stability persist (history kept)."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _state()
    s.levels[Level.L1.value] = LevelRecord(
        status=LevelStatus.VERIFIED, verified_at=t0, stability_days=2.0
    )
    out = recompute(s, t0 + timedelta(days=30), _policy())
    rec = out.levels[Level.L1.value]
    assert rec.status == LevelStatus.EXPIRED
    assert rec.verified_at == t0  # history preserved
    assert rec.stability_days == 2.0
