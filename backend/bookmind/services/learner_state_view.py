"""Learner State view service — LEARNING_MODEL §3, §13; ARCHITECTURE §12.

"Learning State 页面：按已验证、待验证、薄弱、到期分组；当前状态与证据展开。"

This module is the read-side projection that expands a concept's raw
``LearnerConceptState`` into a display-ready structure:

  - per-level *derived effective status* (VERIFIED / BLOCKED_BY_LOWER_LEVEL /
    EXPIRED / UNSTABLE / UNVERIFIED) — derived on read, never written back
    (§5 invariant);
  - per-level retrievability and review-due time, refreshed from the level's
    own timestamp so reads never re-deduct decay (§6);
  - the concept's evidence chain (append-only ledger entries, newest first);
  - a grouping bucket — verified / pending / weak / due — for the Learning
    State page.

It performs no state writes. It is the single place that decides the display
grouping, so the API and (later) the frontend both reference it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..domain.enums import (
    DerivedEffectiveStatus,
    ExposureState,
    Level,
    LevelStatus,
)
from ..domain.models import Concept, Evidence, LearnerConceptState, ReviewPolicy
from ..engine.mastery.state import derived_effective_status
from ..engine.review.forgetting import refresh_record
from ..storage.protocols import Repository

_VERIFYABLE: list[Level] = [Level.L1, Level.L2, Level.L3, Level.L4]

# Display grouping buckets (ARCHITECTURE §12: 已验证/待验证/薄弱/到期).
GROUP_VERIFIED = "verified"
GROUP_PENDING = "pending"     # seen but no level verified yet
GROUP_WEAK = "weak"           # UNSTABLE or BLOCKED_BY_LOWER_LEVEL
GROUP_DUE = "due"             # EXPIRED or review_due in the past


@dataclass
class LevelView:
    level: str
    raw_status: str
    effective_status: str
    retrievability: float
    review_due_at: str | None
    verified_at: str | None
    stability_days: float


@dataclass
class EvidenceView:
    evidence_id: str
    evidence_type: str
    result: str | None
    required_level: str
    independent: bool
    hint_level: int
    occurred_at: str
    task_id: str


@dataclass
class LearnerStateView:
    concept_id: str
    concept_name: str
    exposure: str
    read_progress: float
    current_verified_level: str
    highest_ever_level: str
    levels: list[LevelView]
    evidence: list[EvidenceView]
    group: str  # one of GROUP_*


def build_state_view(
    concept: Concept,
    state: LearnerConceptState,
    evidence: list[Evidence],
    *,
    as_of: datetime,
    policy: ReviewPolicy,
) -> LearnerStateView:
    """Expand one concept's state + evidence into a display view."""
    # Build a refreshed snapshot so derived effective status sees the same
    # decay-derived EXPIRED statuses the display does. We never write this back
    # — it is a read-only projection (§5/§6: reads don't mutate raw state).
    refreshed_state = state.model_copy()
    refreshed_records: dict[str, LevelRecord] = {}
    for lvl in _VERIFYABLE:
        rec = state.level_record(lvl)
        refreshed_records[lvl.value] = refresh_record(lvl, rec, as_of, policy)
    refreshed_state.levels = refreshed_records

    levels: list[LevelView] = []
    for lvl in _VERIFYABLE:
        rec = state.level_record(lvl)
        refreshed = refreshed_records[lvl.value]
        eff = derived_effective_status(lvl, refreshed_state)
        levels.append(LevelView(
            level=lvl.value,
            raw_status=rec.status.value,
            effective_status=eff.value,
            retrievability=round(refreshed.retrievability, 4),
            review_due_at=rec.review_due_at.isoformat() if rec.review_due_at else None,
            verified_at=rec.verified_at.isoformat() if rec.verified_at else None,
            stability_days=rec.stability_days,
        ))

    evidence_views = [
        EvidenceView(
            evidence_id=e.evidence_id,
            evidence_type=e.evidence_type.value,
            result=e.result.value if e.result else None,
            required_level=e.required_level.value,
            independent=e.independent,
            hint_level=int(e.hint_level),
            occurred_at=e.occurred_at.isoformat(),
            task_id=e.task_id,
        )
        for e in sorted(evidence, key=lambda e: e.occurred_at, reverse=True)
    ]

    group = _group(state, levels, as_of)

    return LearnerStateView(
        concept_id=concept.concept_id,
        concept_name=concept.name,
        exposure=state.exposure_state.value,
        read_progress=round(state.read_progress, 4),
        current_verified_level=state.current_verified_level.value,
        highest_ever_level=state.highest_ever_level.value,
        levels=levels,
        evidence=evidence_views,
        group=group,
    )


def _group(state: LearnerConceptState, levels: list[LevelView], as_of: datetime) -> str:
    """Assign one display bucket per concept (ARCHITECTURE §12)."""
    # Due: any level EXPIRED, or any review_due_at in the past.
    for lv in levels:
        if lv.effective_status == DerivedEffectiveStatus.EXPIRED.value:
            return GROUP_DUE
        if lv.review_due_at and datetime.fromisoformat(lv.review_due_at) <= as_of:
            return GROUP_DUE
    # Weak: UNSTABLE or BLOCKED_BY_LOWER_LEVEL.
    for lv in levels:
        if lv.effective_status in (
            DerivedEffectiveStatus.UNSTABLE.value,
            DerivedEffectiveStatus.BLOCKED_BY_LOWER_LEVEL.value,
        ):
            return GROUP_WEAK
    # Verified: at least one level effectively VERIFIED.
    if any(lv.effective_status == DerivedEffectiveStatus.VERIFIED.value for lv in levels):
        return GROUP_VERIFIED
    # Pending: exposed (SEEN/COMPLETED) but nothing verified yet.
    if state.exposure_state in (ExposureState.SEEN, ExposureState.COMPLETED):
        return GROUP_PENDING
    return GROUP_PENDING


def project_state_views(
    repo: Repository,
    project_id: str,
    *,
    as_of: datetime | None = None,
    policy: ReviewPolicy | None = None,
) -> list[LearnerStateView]:
    """Build the full Learning State page for a project: one view per concept."""
    if as_of is None:
        from ..domain.models import utcnow
        as_of = utcnow()
    if policy is None:
        policy = ReviewPolicy()
    out: list[LearnerStateView] = []
    for bid in repo.allowed_book_ids(project_id):
        for c in repo.concepts_for_book(bid):
            state = repo.get_state(project_id, c.concept_id)
            ev = repo.evidence_for(project_id, c.concept_id)
            out.append(build_state_view(c, state, ev, as_of=as_of, policy=policy))
    return out
