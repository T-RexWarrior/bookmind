"""Mastery engine — LEARNING_MODEL.md §5.

Recomputes ``highest_ever_level`` and ``current_verified_level`` from a
concept's effective level records, enforcing the contiguous-validity
invariants:

  1. Each level's raw Evidence and retrievability are stored independently.
  2. ``current_verified_level`` is the highest level starting at L1 whose
     status is continuously VERIFIED (no UNVERIFIED/UNSTABLE/EXPIRED gap).
  3. A lower level that has lapsed does *not* delete higher-level history; the
     higher level's *derived* effective status becomes BLOCKED_BY_LOWER_LEVEL.
  4. ``highest_ever_level`` only ever rises; it is never decremented by
     disconfirmation (LEARNING_MODEL §5 "highest_ever_level 永不因反证删除").

This module is pure: it reads level records + an ``as_of`` time and returns a
recomputed :class:`LearnerConceptState`. It performs no I/O.
"""

from __future__ import annotations

from ...domain.enums import (
    DerivedEffectiveStatus,
    Level,
    LevelStatus,
)
from ...domain.models import LearnerConceptState, LevelRecord, ReviewPolicy
from ..review.forgetting import refresh_record

# L1..L4 in order; L0 is the floor and never a "verified" level.
_VERIFYABLE: list[Level] = [Level.L1, Level.L2, Level.L3, Level.L4]


def _raw_status(record: LevelRecord) -> LevelStatus:
    return record.status


def derived_effective_status(
    level: Level,
    state: LearnerConceptState,
) -> DerivedEffectiveStatus:
    """The *display* status of a level, accounting for lower-level gaps.

    This is derived on read and never written back to ``level_status`` (§5
    invariant: "不写回原始 ``level_status``，避免历史 Evidence 被覆盖").
    """
    record = state.level_record(level)
    raw = record.status
    if raw == LevelStatus.UNVERIFIED:
        return DerivedEffectiveStatus.UNVERIFIED
    if raw == LevelStatus.EXPIRED:
        return DerivedEffectiveStatus.EXPIRED
    if raw == LevelStatus.UNSTABLE:
        return DerivedEffectiveStatus.UNSTABLE
    # raw == VERIFIED: check lower levels are still valid.
    idx = _VERIFYABLE.index(level)
    for lower in _VERIFYABLE[:idx]:
        lower_rec = state.level_record(lower)
        lower_raw = lower_rec.status
        if lower_raw in (LevelStatus.UNVERIFIED, LevelStatus.UNSTABLE, LevelStatus.EXPIRED):
            return DerivedEffectiveStatus.BLOCKED_BY_LOWER_LEVEL
    return DerivedEffectiveStatus.VERIFIED


def recompute(
    state: LearnerConceptState,
    as_of,
    policy: ReviewPolicy,
) -> LearnerConceptState:
    """Return a recomputed state: refresh retrievability/EXPIRED, then derive
    ``current_verified_level`` and keep ``highest_ever_level`` monotonic.
    """
    # 1. Refresh each level's retrievability & EXPIRED status.
    refreshed_records: dict[str, LevelRecord] = {}
    for lvl in _VERIFYABLE:
        rec = state.level_record(lvl)
        refreshed_records[lvl.value] = refresh_record(lvl, rec, as_of, policy)

    # 2. current_verified_level = highest level with a continuous VERIFIED run.
    current = Level.L0
    for lvl in _VERIFYABLE:
        rec = refreshed_records[lvl.value]
        if rec.status == LevelStatus.VERIFIED:
            # Check all lower levels still VERIFIED (contiguity).
            idx = _VERIFYABLE.index(lvl)
            if all(refreshed_records[l.value].status == LevelStatus.VERIFIED for l in _VERIFYABLE[:idx]):
                current = lvl
            else:
                break  # gap found; nothing higher counts
        else:
            break  # first non-verified level stops the run

    # 3. highest_ever_level is monotonic — never decreases.
    highest = state.highest_ever_level
    if _level_rank(current) > _level_rank(highest):
        highest = current

    new_state = state.model_copy()
    new_state.levels = refreshed_records
    new_state.current_verified_level = current
    new_state.highest_ever_level = highest
    return new_state


def _level_rank(level: Level) -> int:
    return _VERIFYABLE.index(level) + 1 if level in _VERIFYABLE else 0


def highest_ever_rank(level: Level) -> int:
    return _level_rank(level)
