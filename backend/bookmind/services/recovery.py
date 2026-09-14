"""Long-term Recovery service — LEARNING_MODEL.md §12, ARCHITECTURE §12.

When a learner has been away for a long time, the system does **not** invent a
parallel state machine. It reuses the same Review candidate selection and the
same Evidence flow as the normal closed loop::

    1. scan high goal_relevance / high importance / strong-prerequisite concepts;
    2. find EXPIRED / UNSTABLE among them;
    3. rank by the §11 lexicographic key (goal_relevance → importance →
       prerequisite_impact → retrievability asc → last_verified earliest →
       concept_id);
    4. offer "3 分钟恢复检查" (a short VERIFY over the top candidates) or
       "直接继续" (resume reading / next-action without a forced check);
    5. the user's choice wins;
    6. recovery tasks produce normal Evidence — there is no separate state.

This module is the read-side projection + the recommendation. The actual
VERIFY task generation goes through the existing Task Validator / submit_answer
path, so this service performs no state writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..domain.enums import (
    Action,
    ActivityMode,
    Difficulty,
    InterventionPolicy,
    Level,
    LevelStatus,
)
from ..domain.models import Concept, LearnerConceptState, ReviewPolicy
from ..engine.decision.next_action import ConceptView
from ..engine.mastery.state import derived_effective_status
from ..engine.review.forgetting import refresh_record
from ..domain.enums import DerivedEffectiveStatus
from ..storage.protocols import Repository


# A concept is "recovery-relevant" if it is high-value along any of the three
# axes LEARNING_MODEL §12 names: goal_relevance, importance, strong prerequisite
# of a pending concept. The thresholds mirror the decision engine's own cut-offs
# (rule 8 uses goal_relevance >= 0.7 for "key concept").
GOAL_RELEVANCE_THRESHOLD = 0.7
IMPORTANCE_THRESHOLD = 0.7


@dataclass
class RecoveryCandidate:
    """One concept the learner may want to recover on return."""

    concept_id: str
    concept_name: str
    chapter: str
    reason: str  # "EXPIRED" | "UNSTABLE"
    current_verified_level: str
    highest_ever_level: str
    retrievability: float
    last_verified_at: str | None
    # The §11 sort keys, surfaced for the trace / UI.
    goal_relevance: float
    importance: float
    is_strong_prerequisite: bool


@dataclass
class RecoveryPlan:
    """The Recovery page projection (ARCHITECTURE §12)."""

    project_id: str
    days_away: float
    candidates: list[RecoveryCandidate] = field(default_factory=list)
    recommendation: str = ""  # "recovery_check" | "continue"
    rationale: str = ""
    # User's last choice, recorded so the trace is auditable. The service never
    # forces a choice; it only recommends.
    user_choice: str | None = None  # "recovery_check" | "continue" | None

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "days_away": round(self.days_away, 2),
            "candidates": [c.__dict__ for c in self.candidates],
            "recommendation": self.recommendation,
            "rationale": self.rationale,
            "user_choice": self.user_choice,
        }


def _last_verified_timestamp(state: LearnerConceptState) -> datetime | None:
    times = [
        state.level_record(lvl).verified_at
        for lvl in (Level.L1, Level.L2, Level.L3, Level.L4)
        if state.level_record(lvl).verified_at is not None
    ]
    return max(times) if times else None


def _is_recovery_relevant(concept: Concept, state: LearnerConceptState) -> bool:
    """High goal_relevance, high importance, or a strong prerequisite of a
    concept the learner has been working on (exposure SEEN/COMPLETED)."""
    if concept.goal_relevance >= GOAL_RELEVANCE_THRESHOLD:
        return True
    if concept.importance >= IMPORTANCE_THRESHOLD:
        return True
    # Strong prerequisite of an exposed concept: the concept has prerequisites
    # and the learner has seen at least one dependent. We approximate "strong"
    # as importance >= 0.6 (prereqs the skeleton rates highly).
    if concept.importance >= 0.6 and concept.prerequisites:
        return True
    return False


def _needs_recovery(refreshed_status: str) -> str | None:
    """Return 'EXPIRED' / 'UNSTABLE' if the level needs recovery, else None.

    We check the *derived effective status* so BLOCKED_BY_LOWER_LEVEL is not
    mistaken for a recovery target in its own right — the blocking lower level
    is the real target.
    """
    if refreshed_status == DerivedEffectiveStatus.EXPIRED.value:
        return "EXPIRED"
    if refreshed_status == DerivedEffectiveStatus.UNSTABLE.value:
        return "UNSTABLE"
    return None


def _candidate_sort_key(c: RecoveryCandidate) -> tuple:
    """The §11 lexicographic key, mirroring ConceptView.tie_sort_key.

    goal_relevance desc, importance desc, prerequisite_impact (we use the
    is_strong_prerequisite flag as a 0/1 impact proxy) desc, retrievability
    asc, last_verified earliest-first, concept_id asc.
    """
    lv = c.last_verified_at or ""  # empty sorts before any ISO timestamp
    return (
        -c.goal_relevance,
        -c.importance,
        -(1 if c.is_strong_prerequisite else 0),
        c.retrievability,
        lv,
        c.concept_id,
    )


class RecoveryService:
    """Builds the Recovery-page recommendation. Read-only; no state writes.

    A single instance is fine — it holds no per-turn mutable state.
    """

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def build_plan(
        self,
        *,
        project_id: str,
        as_of: datetime | None = None,
        policy: ReviewPolicy | None = None,
        last_active_at: datetime | None = None,
    ) -> RecoveryPlan:
        """Scan the project's concepts for recovery candidates and recommend.

        ``last_active_at`` is the learner's last session time, used to compute
        ``days_away``. If unknown, it is derived from the most recent Evidence
        timestamp in the project.
        """
        from ..domain.models import utcnow

        if as_of is None:
            as_of = utcnow()
        if policy is None:
            policy = ReviewPolicy()
        if last_active_at is None:
            last_active_at = self._derive_last_active(project_id) or as_of
        # Normalize to aware UTC: SQLite may store offset-naive timestamps.
        if last_active_at.tzinfo is None:
            from datetime import timezone
            last_active_at = last_active_at.replace(tzinfo=timezone.utc)
        days_away = max(0.0, (as_of - last_active_at).total_seconds() / 86400.0)

        candidates: list[RecoveryCandidate] = []
        for bid in self.repo.allowed_book_ids(project_id):
            for concept in self.repo.concepts_for_book(bid):
                state = self.repo.get_state(project_id, concept.concept_id)
                if not _is_recovery_relevant(concept, state):
                    continue
                # Find the highest level that is EXPIRED/UNSTABLE (derived).
                worst_reason: str | None = None
                worst_retrievability = 1.0
                worst_level = Level.L0
                for lvl in (Level.L1, Level.L2, Level.L3, Level.L4):
                    rec = state.level_record(lvl)
                    refreshed = refresh_record(lvl, rec, as_of, policy)
                    # Use a temp state copy so derived_effective_status sees lower
                    # levels consistently.
                    tmp = state.model_copy()
                    tmp.set_level_record(lvl, refreshed)
                    eff = derived_effective_status(lvl, tmp)
                    reason = _needs_recovery(eff.value)
                    if reason is not None:
                        if worst_reason is None or lvl.value > worst_level.value:
                            worst_reason = reason
                            worst_retrievability = refreshed.retrievability
                            worst_level = lvl
                if worst_reason is None:
                    continue
                lv_ts = _last_verified_timestamp(state)
                candidates.append(RecoveryCandidate(
                    concept_id=concept.concept_id,
                    concept_name=concept.name,
                    chapter=concept.chapter,
                    reason=worst_reason,
                    current_verified_level=state.current_verified_level.value,
                    highest_ever_level=state.highest_ever_level.value,
                    retrievability=round(worst_retrievability, 4),
                    last_verified_at=lv_ts.isoformat() if lv_ts else None,
                    goal_relevance=concept.goal_relevance,
                    importance=concept.importance,
                    is_strong_prerequisite=concept.importance >= 0.6 and bool(concept.prerequisites),
                ))

        candidates.sort(key=_candidate_sort_key)

        recommendation, rationale = self._recommend(candidates, days_away)
        return RecoveryPlan(
            project_id=project_id,
            days_away=days_away,
            candidates=candidates,
            recommendation=recommendation,
            rationale=rationale,
        )

    def record_choice(self, plan: RecoveryPlan, choice: str) -> RecoveryPlan:
        """Record the user's choice (LEARNING_MODEL §12: 用户选择优先).

        ``choice`` is ``"recovery_check"`` or ``"continue"``. The service does
        not override the choice; it only stamps it on the plan for auditability.
        """
        if choice not in ("recovery_check", "continue"):
            raise ValueError(f"choice must be 'recovery_check' or 'continue', got {choice!r}")
        plan.user_choice = choice
        return plan

    def recommend_action(self, plan: RecoveryPlan) -> tuple[Action, str | None]:
        """Map the (recommendation, user_choice) to a concrete next Action and
        target concept, for the decision trace.

        - If the user chose ``continue`` (or there are no candidates), the
          system respects that and returns the normal next-action path
          (CONTINUE_READING / WAIT) — it does **not** force a check.
        - If the user chose ``recovery_check`` (or accepted the recommendation
          and candidates exist), VERIFY the top candidate.

        This never bypasses the mode action matrix; the caller runs the result
        through ``decide`` for legality.
        """
        # User choice wins (§12.5).
        effective = plan.user_choice or plan.recommendation
        if effective == "recovery_check" and plan.candidates:
            top = plan.candidates[0]
            return Action.VERIFY, top.concept_id
        # "continue" or no candidates → no forced check.
        if plan.days_away <= 0:
            return Action.CONTINUE_READING, None
        return Action.CONTINUE_READING, None

    # --- helpers ---------------------------------------------------------

    def _derive_last_active(self, project_id: str) -> datetime | None:
        times = [
            e.occurred_at for e in self.repo.evidence_for_project(project_id)
        ]
        return max(times) if times else None

    def _recommend(
        self, candidates: list[RecoveryCandidate], days_away: float
    ) -> tuple[str, str]:
        """Decide whether to recommend a recovery check or just continuing.

        Heuristic (explainable, no opaque score): recommend a 3-minute recovery
        check iff the learner has been away long enough that decay is plausible
        AND there is at least one high-value EXPIRED/UNSTABLE concept. Otherwise
        recommend continuing — the normal closed loop already handles UNSTABLE
        via rule 8 when the learner is active.
        """
        if not candidates:
            return "continue", "no high-value concepts need recovery; continue the normal loop"
        # Long enough away that a check is worth the interruption. 2 days lines
        # up with the default L1 initial stability (2 days) — past one L1
        # half-life, retrieval is meaningfully decayed.
        if days_away < 2.0:
            return "continue", (
                f"only {days_away:.1f} days away — decay is mild; the normal "
                "REVIEW rule (§11.8) will handle it when active"
            )
        expired = [c for c in candidates if c.reason == "EXPIRED"]
        if expired:
            return "recovery_check", (
                f"{len(candidates)} high-value concept(s) need attention "
                f"({len(expired)} EXPIRED) after {days_away:.0f} days away; "
                "a 3-minute recovery check is recommended, or continue directly"
            )
        return "recovery_check", (
            f"{len(candidates)} high-value concept(s) are UNSTABLE after "
            f"{days_away:.0f} days away; a short recovery check is recommended"
        )


def project_recovery_plan(
    repo: Repository,
    project_id: str,
    *,
    as_of: datetime | None = None,
    policy: ReviewPolicy | None = None,
    last_active_at: datetime | None = None,
) -> RecoveryPlan:
    """Convenience entry point mirroring ``project_state_views``."""
    return RecoveryService(repo).build_plan(
        project_id=project_id, as_of=as_of, policy=policy, last_active_at=last_active_at,
    )
