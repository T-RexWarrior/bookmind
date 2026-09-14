"""L1 tests: forgetting curve — LEARNING_MODEL.md §6.

R_k(t) = (1 + t / (9 * S_k)) ** -1

Fixed-timepoint checks: at t=0 R=1.0; at t=S_k R=0.9; expiry is R<threshold.
These tests must be fully deterministic (no LLM, no clock drift).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bookmind.domain.enums import Level, LevelStatus
from bookmind.domain.models import LevelRecord, ReviewPolicy
from bookmind.engine.review.forgetting import (
    is_expired,
    refresh_record,
    reschedule_after_fail,
    reschedule_after_partial,
    retrievability,
    review_due_after_pass,
    stability_after_independent_pass,
)


def _policy() -> ReviewPolicy:
    return ReviewPolicy()


def test_retrievability_at_zero_days_is_one():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    r = retrievability(t0, stability_days=4.0, as_of=t0, policy=_policy())
    assert r == 1.0


def test_retrievability_at_one_stability_is_threshold():
    """At t = S_k, R should equal the expiry threshold (0.9) by construction."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    as_of = t0 + timedelta(days=4.0)  # S_k = 4 for L2
    r = retrievability(t0, stability_days=4.0, as_of=as_of, policy=_policy())
    assert abs(r - 0.9) < 1e-6


def test_retrievability_decreases_monotonically():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    r1 = retrievability(t0, 4.0, t0 + timedelta(days=1), _policy())
    r2 = retrievability(t0, 4.0, t0 + timedelta(days=10), _policy())
    r3 = retrievability(t0, 4.0, t0 + timedelta(days=100), _policy())
    assert r1 > r2 > r3 > 0.0


def test_retrievability_never_verified_is_zero():
    r = retrievability(None, 4.0, datetime(2025, 1, 1, tzinfo=timezone.utc), _policy())
    assert r == 0.0


def test_retrievability_zero_stability_is_zero():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert retrievability(t0, 0.0, t0, _policy()) == 0.0


def test_expired_when_below_threshold():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    # S=4 → R=0.9 at t=4; t=10 is well below 0.9.
    far = t0 + timedelta(days=10)
    assert is_expired(t0, 4.0, far, _policy()) is True


def test_not_expired_just_above_threshold():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    near = t0 + timedelta(days=3)  # R slightly above 0.9
    assert is_expired(t0, 4.0, near, _policy()) is False


def test_unverified_is_not_expired():
    assert is_expired(None, 4.0, datetime(2025, 1, 1, tzinfo=timezone.utc), _policy()) is False


def test_refresh_record_marks_verified_expired():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rec = LevelRecord(
        status=LevelStatus.VERIFIED,
        verified_at=t0,
        stability_days=4.0,
    )
    far = t0 + timedelta(days=30)
    out = refresh_record(Level.L2, rec, far, _policy())
    assert out.status == LevelStatus.EXPIRED
    assert out.retrievability < 0.9


def test_refresh_record_does_not_mark_unstable_expired():
    """UNSTABLE is a lifecycle status, not decay — refresh must not override it."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rec = LevelRecord(status=LevelStatus.UNSTABLE, verified_at=t0, stability_days=4.0)
    out = refresh_record(Level.L2, rec, t0 + timedelta(days=30), _policy())
    assert out.status == LevelStatus.UNSTABLE


def test_stability_after_pass_grows():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    as_of = t0 + timedelta(days=5)
    # S_new = max(4, 5) * 1.8 = 9.0
    s = stability_after_independent_pass(4.0, t0, as_of, _policy())
    assert abs(s - 9.0) < 1e-9


def test_stability_after_pass_floor():
    """If very little time passed, S grows from the old floor, not elapsed."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    as_of = t0 + timedelta(hours=1)  # ~0 days elapsed
    # S_new = max(10, ~0) * 1.8 = 18.0
    s = stability_after_independent_pass(10.0, t0, as_of, _policy())
    assert abs(s - 18.0) < 1e-6


def test_reschedule_after_partial_one_day():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    due = reschedule_after_partial(t0, t0, _policy())
    assert due == t0 + timedelta(days=1)


def test_reschedule_after_fail_capped_at_one_day():
    as_of = datetime(2025, 1, 1, tzinfo=timezone.utc)
    due = reschedule_after_fail(old_stability_days=100.0, as_of=as_of, policy=_policy())
    # min(1, 0.25*100=25) = 1 day
    assert due == as_of + timedelta(days=1)


def test_reschedule_after_fail_small_stability():
    as_of = datetime(2025, 1, 1, tzinfo=timezone.utc)
    due = reschedule_after_fail(old_stability_days=2.0, as_of=as_of, policy=_policy())
    # min(1, 0.25*2=0.5) = 0.5 day
    assert due == as_of + timedelta(days=0.5)


def test_review_due_after_pass_hits_threshold():
    """The scheduled review time should be exactly when R drops to threshold."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = 4.0
    due = review_due_after_pass(t0, s, t0, _policy())
    # At due, R should equal the threshold (0.9).
    r = retrievability(t0, s, due, _policy())
    assert abs(r - 0.9) < 1e-6


def test_reading_state_never_deducts_decay_repeatedly():
    """Refresh is idempotent: calling twice with the same as_of yields same R."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rec = LevelRecord(status=LevelStatus.VERIFIED, verified_at=t0, stability_days=4.0)
    as_of = t0 + timedelta(days=5)
    once = refresh_record(Level.L2, rec, as_of, _policy())
    twice = refresh_record(Level.L2, once, as_of, _policy())
    assert once.retrievability == twice.retrievability
    assert once.status == twice.status
