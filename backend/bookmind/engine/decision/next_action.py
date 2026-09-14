"""Next Best Action — LEARNING_MODEL.md §11.

Constraint-and-rules-first. The engine:

  1. filters actions to those legal for ``(activity_mode, intervention_policy)``
     and the source (system-proactive vs user-requested);
  2. applies the 12 ordered rules; the first rule that yields a candidate wins;
  3. among same-rule candidates, breaks ties with the fixed lexicographic key:

         goal_relevance (desc) → importance (desc) → prerequisite_impact (desc)
         → retrievability (asc) → last_verified (earliest/None first)
         → concept_id (asc, stable terminator)

No opaque composite score is ever produced.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.enums import Action, ActivityMode, InterventionPolicy, Level, LevelStatus, MisconceptionStatus
from ...domain.models import Concept, DecisionCandidate, DecisionTrace, LearnerConceptState, MisconceptionHypothesis
from ..action_matrix import legal_system_actions, legal_user_actions


# Sort-key priorities. True = descending.
_TIE_KEYS: list[tuple[str, bool]] = [
    ("goal_relevance", True),
    ("importance", True),
    ("prerequisite_impact", True),
    ("retrievability", False),  # ascending: lower retrievability = more urgent
    ("last_verified", False),   # earliest/None first
    ("concept_id", False),      # stable terminator, ascending
]


@dataclass
class ConceptView:
    """A flattened view joining concept + learner state for decision-making."""

    concept: Concept
    state: LearnerConceptState
    # Derived flags the decision layer pre-computes.
    is_strong_prerequisite_for_pending: bool = False
    prerequisite_impact: float = 0.0
    has_unverified_strong_prerequisite: bool = False

    @property
    def concept_id(self) -> str:
        return self.concept.concept_id

    @property
    def goal_relevance(self) -> float:
        return self.concept.goal_relevance

    @property
    def importance(self) -> float:
        return self.concept.importance

    @property
    def retrievability(self) -> float:
        # Max retrievability across verified levels (0 if none).
        return max(
            (self.state.level_record(lvl).retrievability for lvl in (Level.L1, Level.L2, Level.L3, Level.L4)),
            default=0.0,
        )

    @property
    def last_verified(self) -> float:
        # Epoch timestamp of most recent verification, or -inf (sorts first).
        times = [
            self.state.level_record(lvl).verified_at
            for lvl in (Level.L1, Level.L2, Level.L3, Level.L4)
            if self.state.level_record(lvl).verified_at is not None
        ]
        if not times:
            return float("-inf")
        return max(t.timestamp() for t in times)

    def tie_sort_key(self) -> tuple:
        cid = self.concept_id
        # For ascending last_verified with "None first", -inf already sorts first.
        return (
            -self.goal_relevance,
            -self.importance,
            -self.prerequisite_impact,
            self.retrievability,
            self.last_verified,
            cid,
        )


@dataclass
class DecisionInput:
    activity_mode: ActivityMode
    intervention_policy: InterventionPolicy
    ui_preset: str
    concepts: list[ConceptView]
    misconceptions: list[MisconceptionHypothesis]
    user_requested_action: Action | None = None
    user_requested_concept_id: str | None = None
    # True when the user explicitly asked to be tested ("考考我"). This does NOT
    # force a specific action (user_requested_action does); it only unlocks the
    # proactive rules in QUIET mode so the highest-value task can surface. A
    # user who asks is never blocked by QUIET (action_matrix §10).
    user_requested: bool = False
    has_active_reading_passage: bool = False
    chapter_just_ended: bool = False
    key_concepts_unverified: bool = False
    # concept_ids that are strong prerequisites of concepts the learner is about to study.
    pending_target_concept_ids: set[str] | None = None

    def concept_by_id(self, cid: str) -> ConceptView | None:
        for c in self.concepts:
            if c.concept_id == cid:
                return c
        return None


def _status_of(mis: MisconceptionHypothesis) -> MisconceptionStatus:
    return mis.status


def _has_competing(mis: MisconceptionHypothesis, all_mis: list[MisconceptionHypothesis]) -> bool:
    """True if another hypothesis in the same group is still active (not DISMISSED/RESOLVED)."""
    if mis.hypothesis_group is None:
        return False
    peers = [m for m in all_mis if m.hypothesis_group == mis.hypothesis_group and m.bug_id != mis.bug_id]
    return any(m.status not in (MisconceptionStatus.DISMISSED, MisconceptionStatus.RESOLVED) for m in peers)


def _make_candidate(view: ConceptView, action: Action, rule_index: int) -> DecisionCandidate:
    return DecisionCandidate(
        concept_id=view.concept_id,
        action=action.value,
        sort_keys={
            "goal_relevance": view.goal_relevance,
            "importance": view.importance,
            "prerequisite_impact": view.prerequisite_impact,
            "retrievability": view.retrievability,
            "last_verified": view.last_verified,
        },
        rule_index=rule_index,
    )


def _best(candidates: list[ConceptView], legal_action: Action, rule_index: int) -> tuple[DecisionCandidate | None, ConceptView | None]:
    if not candidates:
        return None, None
    candidates_sorted = sorted(candidates, key=lambda v: v.tie_sort_key())
    winner = candidates_sorted[0]
    return _make_candidate(winner, legal_action, rule_index), winner


def _level_status(view: ConceptView, level: Level) -> LevelStatus:
    return view.state.level_record(level).status


def decide(input_: DecisionInput) -> DecisionTrace:
    """Apply the 12 ordered rules; return a trace explaining the choice."""
    am, ip = input_.activity_mode, input_.intervention_policy
    sys_actions = legal_system_actions(am, ip)
    user_actions = legal_user_actions(am, ip)
    # When the user explicitly requests a task (user_requested=True, or a
    # specific user_requested_action), the proactive rules (2–8) may run against
    # the *user*-allowed action set. This lets a CONFIRMED misconception still
    # surface REMEDIATE, or an active hypothesis surface DIAGNOSE, even in QUIET
    # mode — QUIET never blocks a user who asks (action_matrix §10).
    effective_sys = (
        user_actions if (input_.user_requested or input_.user_requested_action is not None)
        else sys_actions
    )

    checked: list[int] = []
    candidates_all: list[DecisionCandidate] = []

    def consider(rule_index: int, cand: DecisionCandidate | None, view: ConceptView | None):
        checked.append(rule_index)
        if cand is not None:
            candidates_all.append(cand)
        return cand, view

    selected_action: Action | None = None
    selected_concept: str | None = None
    selected_rule = -1
    reason = ""

    # Rule 1: user-requested action (takes priority over proactive rules — a
    # specific action the user asks for, e.g. ANSWER, is honoured first).
    if input_.user_requested_action is not None:
        ua = input_.user_requested_action
        if ua in user_actions:
            selected_action = ua
            selected_concept = input_.user_requested_concept_id
            selected_rule = 1
            reason = f"user requested {ua.value}"
            checked.append(1)
        else:
            reason = f"user requested {ua.value} is not legal in {am.value}+{ip.value}; suggest mode switch"
            checked.append(1)
            # Fall through to system rules but flag illegality via WAIT eventually.
    # Rule 2: RELAPSED with competing hypothesis → DIAGNOSE.
    if selected_action is None:
        relapsed = [m for m in input_.misconceptions if m.status == MisconceptionStatus.RELAPSED and _has_competing(m, input_.misconceptions)]
        if relapsed and Action.DIAGNOSE in effective_sys:
            # pick the concept of the first relapsed bug
            mis = relapsed[0]
            cand, view = _best([c for c in input_.concepts if c.concept_id in mis.related_concepts], Action.DIAGNOSE, 2)
            cand, view = consider(2, cand, view)
            if cand:
                selected_action, selected_concept, selected_rule = Action.DIAGNOSE, cand.concept_id, 2
                reason = "RELAPSED misconception with competing hypothesis → DIAGNOSE"

    # Rule 3: CONFIRMED, or RELAPSED without competing → REMEDIATE.
    if selected_action is None:
        targets = [m for m in input_.misconceptions if m.status == MisconceptionStatus.CONFIRMED or (m.status == MisconceptionStatus.RELAPSED and not _has_competing(m, input_.misconceptions))]
        if targets and Action.REMEDIATE in effective_sys:
            mis = targets[0]
            cand, view = _best([c for c in input_.concepts if c.concept_id in mis.related_concepts], Action.REMEDIATE, 3)
            cand, view = consider(3, cand, view)
            if cand:
                selected_action, selected_concept, selected_rule = Action.REMEDIATE, cand.concept_id, 3
                reason = "CONFIRMED/RELAPSED misconception → REMEDIATE"

    # Rule 4: REMEDIATING with correction incomplete → REMEDIATE.
    if selected_action is None:
        rem = [m for m in input_.misconceptions if m.status == MisconceptionStatus.REMEDIATING]
        if rem and Action.REMEDIATE in effective_sys:
            mis = rem[0]
            cand, view = _best([c for c in input_.concepts if c.concept_id in mis.related_concepts], Action.REMEDIATE, 4)
            cand, view = consider(4, cand, view)
            if cand:
                selected_action, selected_concept, selected_rule = Action.REMEDIATE, cand.concept_id, 4
                reason = "REMEDIATING, correction not finished → REMEDIATE"

    # Rule 5: REMEDIATING-correction-done, or VERIFYING → VERIFY changed task.
    if selected_action is None:
        # REMEDIATING-with-correction-done is signalled by the service layer
        # moving the hypothesis to VERIFYING; we handle VERIFYING here.
        verify_mis = [m for m in input_.misconceptions if m.status in (MisconceptionStatus.VERIFYING,)]
        if verify_mis and Action.VERIFY in effective_sys:
            mis = verify_mis[0]
            cand, view = _best([c for c in input_.concepts if c.concept_id in mis.related_concepts], Action.VERIFY, 5)
            cand, view = consider(5, cand, view)
            if cand:
                selected_action, selected_concept, selected_rule = Action.VERIFY, cand.concept_id, 5
                reason = "VERIFYING changed task → VERIFY"

    # Rule 6: LIKELY with competing hypothesis → DIAGNOSE.
    if selected_action is None:
        likely = [m for m in input_.misconceptions if m.status == MisconceptionStatus.LIKELY and _has_competing(m, input_.misconceptions)]
        if likely and Action.DIAGNOSE in effective_sys:
            mis = likely[0]
            cand, view = _best([c for c in input_.concepts if c.concept_id in mis.related_concepts], Action.DIAGNOSE, 6)
            cand, view = consider(6, cand, view)
            if cand:
                selected_action, selected_concept, selected_rule = Action.DIAGNOSE, cand.concept_id, 6
                reason = "LIKELY misconception with competing hypothesis → DIAGNOSE"

    # Rule 7: pending concept has L0/UNVERIFIED strong prerequisite → LEARN_PREREQUISITE.
    if selected_action is None:
        prereq_views = [c for c in input_.concepts if c.has_unverified_strong_prerequisite]
        if prereq_views and Action.LEARN_PREREQUISITE in effective_sys:
            cand, view = _best(prereq_views, Action.LEARN_PREREQUISITE, 7)
            cand, view = consider(7, cand, view)
            if cand:
                selected_action, selected_concept, selected_rule = Action.LEARN_PREREQUISITE, cand.concept_id, 7
                reason = "pending concept has unverified strong prerequisite → LEARN_PREREQUISITE"

    # Rule 8: key prerequisite or high-goal concept UNSTABLE/EXPIRED → REVIEW.
    if selected_action is None:
        review_views = [
            c for c in input_.concepts
            if (c.is_strong_prerequisite_for_pending or c.goal_relevance >= 0.7)
            and any(_level_status(c, lvl) in (LevelStatus.UNSTABLE, LevelStatus.EXPIRED) for lvl in (Level.L1, Level.L2, Level.L3, Level.L4))
        ]
        if review_views and Action.REVIEW in effective_sys:
            cand, view = _best(review_views, Action.REVIEW, 8)
            cand, view = consider(8, cand, view)
            if cand:
                selected_action, selected_concept, selected_rule = Action.REVIEW, cand.concept_id, 8
                reason = "key prerequisite/goal concept UNSTABLE/EXPIRED → REVIEW"

    # Rule 9: READING+PROACTIVE, chapter ended, key concept unverified → VERIFY.
    if selected_action is None:
        if (
            am == ActivityMode.READING
            and ip == InterventionPolicy.PROACTIVE
            and input_.chapter_just_ended
            and input_.key_concepts_unverified
            and Action.VERIFY in effective_sys
        ):
            verify_views = [
                c for c in input_.concepts
                if c.goal_relevance >= 0.5
                and all(_level_status(c, lvl) == LevelStatus.UNVERIFIED for lvl in (Level.L1,))
            ]
            cand, view = _best(verify_views, Action.VERIFY, 9)
            cand, view = consider(9, cand, view)
            if cand:
                selected_action, selected_concept, selected_rule = Action.VERIFY, cand.concept_id, 9
                reason = "chapter ended, key concept unverified → VERIFY"

    # Rule 10: ASSESSMENT → VERIFY at goal level.
    if selected_action is None:
        if am == ActivityMode.ASSESSMENT and Action.VERIFY in sys_actions:
            assess_views = [c for c in input_.concepts if c.goal_relevance > 0.0]
            cand, view = _best(assess_views, Action.VERIFY, 10)
            cand, view = consider(10, cand, view)
            if cand:
                selected_action, selected_concept, selected_rule = Action.VERIFY, cand.concept_id, 10
                reason = "ASSESSMENT → VERIFY at goal level"

    # Rule 11: READING with active passage → CONTINUE_READING.
    if selected_action is None:
        if am == ActivityMode.READING and input_.has_active_reading_passage and Action.CONTINUE_READING in sys_actions:
            cand = DecisionCandidate(concept_id="", action=Action.CONTINUE_READING.value, sort_keys={}, rule_index=11)
            consider(11, cand, None)
            selected_action, selected_rule = Action.CONTINUE_READING, 11
            reason = "READING with active passage → CONTINUE_READING"

    # Rule 11.5: user-requested action fallback. If no proactive rule produced
    # a candidate but the user explicitly asked for an action (e.g. "考考我"),
    # honour it as long as it's legal for the user in this mode. A bare
    # user_requested (no specific action) falls back to VERIFY so the user gets
    # a task. QUIET never blocks a user who asks (action_matrix §10).
    if selected_action is None and (input_.user_requested or input_.user_requested_action is not None):
        ua = input_.user_requested_action
        if ua is None:
            ua = Action.VERIFY
        if ua in user_actions:
            selected_action = ua
            selected_concept = input_.user_requested_concept_id
            selected_rule = 1
            reason = f"user requested {ua.value}"
        else:
            reason = f"user requested {ua.value} is not legal in {am.value}+{ip.value}; suggest mode switch"

    # Rule 12: WAIT.
    if selected_action is None:
        cand = DecisionCandidate(concept_id="", action=Action.WAIT.value, sort_keys={}, rule_index=12)
        consider(12, cand, None)
        selected_action, selected_rule = Action.WAIT, 12
        if not reason:
            reason = "no legal positive-value action, or proactive would violate QUIET → WAIT"

    return DecisionTrace(
        project_id="",
        activity_mode=am,
        intervention_policy=ip,
        ui_preset=input_.ui_preset,  # type: ignore[arg-type]
        checked_rules=checked,
        selected_rule=selected_rule,
        candidates=candidates_all,
        selected_action=selected_action.value if selected_action else Action.WAIT.value,
        selected_concept_id=selected_concept,
        reason=reason,
    )
