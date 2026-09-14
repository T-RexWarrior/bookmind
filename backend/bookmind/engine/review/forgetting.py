"""Forgetting & review scheduling — LEARNING_MODEL.md §6.

A deliberately simple, explainable curve — *not* full FSRS, never claimed to be
calibrated on real students::

    R_k(t) = (1 + t / (9 * S_k)) ** -1

where ``t`` is days since the last independent verification of level ``k`` and
``S_k`` is that level's stability in days (R = 0.9 at t = S_k).

This module is pure: given a state + ``as_of`` time + a :class:`ReviewPolicy`
it computes retrievability, expiry and the next review time. It never mutates
state in place — callers apply the returned record.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from ...domain.enums import Level, LevelStatus
from ...domain.models import LevelRecord, ReviewPolicy


def retrievability(
    last_verified_at: datetime | None,
    stability_days: float,
    as_of: datetime,
    policy: ReviewPolicy,
) -> float:
    """Power-law retention, clamped to [0, 1]."""
    if last_verified_at is None:
        return 0.0
    if stability_days <= 0:
        return 0.0
    elapsed_days = max(0.0, (as_of - last_verified_at).total_seconds() / 86400.0)
    scale = policy.retrievability_scale_days * stability_days
    if scale <= 0:
        return 0.0
    r = 1.0 / (1.0 + elapsed_days / scale)
    return max(0.0, min(1.0, r))


def is_expired(
    last_verified_at: datetime | None,
    stability_days: float,
    as_of: datetime,
    policy: ReviewPolicy,
) -> bool:
    if last_verified_at is None:
        return False  # UNVERIFIED is not EXPIRED
    if stability_days <= 0:
        return False
    r = retrievability(last_verified_at, stability_days, as_of, policy)
    return r < policy.expiry_retrievability_threshold


def initial_stability(level: Level, policy: ReviewPolicy) -> float:
    return policy.initial_stability_days.get(level.value, 0.0)


def _elapsed_days(verified_at: datetime | None, as_of: datetime) -> float:
    if verified_at is None:
        return 0.0
    return max(0.0, (as_of - verified_at).total_seconds() / 86400.0)


def stability_after_independent_pass(
    old_stability_days: float,
    verified_at: datetime | None,
    as_of: datetime,
    policy: ReviewPolicy,
) -> float:
    """``S_new = max(S_old, elapsed_days) * pass_multiplier``."""
    elapsed = _elapsed_days(verified_at, as_of)
    base = max(old_stability_days, elapsed)
    return base * policy.pass_multiplier


def reschedule_after_partial(verified_at: datetime | None, as_of: datetime, policy: ReviewPolicy) -> datetime:
    """PARTIAL or hinted PASS: keep evidence, reschedule ~1 day later."""
    start = verified_at or as_of
    return start + timedelta(days=policy.partial_reschedule_days)


def reschedule_after_fail(
    old_stability_days: float,
    as_of: datetime,
    policy: ReviewPolicy,
) -> datetime:
    """Independent FAIL → UNSTABLE; review at ``min(1 day, 0.25 * S_old)``."""
    gap = min(1.0, policy.fail_reschedule_factor * max(old_stability_days, 0.0))
    return as_of + timedelta(days=gap)


def review_due_after_pass(
    verified_at: datetime,
    new_stability_days: float,
    as_of: datetime,
    policy: ReviewPolicy,
) -> datetime:
    """Next review scheduled where retrievability will hit the expiry threshold.

    Solving ``R = threshold`` for ``t``::

        threshold = 1 / (1 + t / (9S))  ⇒  t = 9S * (1/threshold - 1)
    """
    if new_stability_days <= 0 or policy.expiry_retrievability_threshold <= 0:
        return as_of
    scale = policy.retrievability_scale_days * new_stability_days
    t_days = scale * (1.0 / policy.expiry_retrievability_threshold - 1.0)
    t_days = max(0.0, t_days)
    return verified_at + timedelta(days=t_days)


def refresh_record(
    level: Level,
    record: LevelRecord,
    as_of: datetime,
    policy: ReviewPolicy,
) -> LevelRecord:
    """Recompute ``retrievability`` and EXPIRED status for an existing record.

    Reading state must never re-deduct decay, so this derives ``retrievability``
    fresh from the record's own timestamp each call.
    """
    new = record.model_copy()
    r = retrievability(record.verified_at, record.stability_days, as_of, policy)
    new.retrievability = round(r, 6)
    if (
        record.status == LevelStatus.VERIFIED
        and is_expired(record.verified_at, record.stability_days, as_of, policy)
    ):
        new.status = LevelStatus.EXPIRED
    return new
