"""GoldenCase replay harness — runs each case through the decision engine and
checks allowed/forbidden actions and the expected action.

This is the L3-style deterministic replay (EVALUATION.md §4.1 "离线固定轨迹"):
same initial state → same expected action. No LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import Action, Level, LevelStatus
from ..domain.models import Concept, LearnerConceptState
from ..engine.decision.next_action import ConceptView, DecisionInput, decide
from .golden_cases import GOLDEN_CASES, GoldenCase


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    selected_action: str
    expected_action: str
    failures: list[str] = field(default_factory=list)


def _build_concept_view(state: LearnerConceptState) -> ConceptView:
    concept = Concept(
        concept_id=state.concept_id,
        book_id="b1",
        name=state.concept_id,
        importance=0.5,
        goal_relevance=state.goal_relevance,
    )
    return ConceptView(concept=concept, state=state)


def _mark_prereq_flags(views: list[ConceptView], case: GoldenCase) -> None:
    """Set has_unverified_strong_prerequisite on prerequisites of the pending
    target that are still UNVERIFIED, and is_strong_prerequisite_for_pending
    on those prerequisites."""
    if not case.pending_concept_id:
        return
    from ..agents.concept_skeleton import PREREQUISITE_EDGES
    pres = PREREQUISITE_EDGES.get(case.pending_concept_id, [])
    for v in views:
        if v.concept_id in pres:
            v.is_strong_prerequisite_for_pending = True
            if v.state.level_record(Level.L1).status == LevelStatus.UNVERIFIED:
                v.has_unverified_strong_prerequisite = True


def run_case(case: GoldenCase) -> CaseResult:
    views = [_build_concept_view(s) for s in case.initial_states]
    _mark_prereq_flags(views, case)

    inp = DecisionInput(
        activity_mode=case.activity_mode,
        intervention_policy=case.intervention_policy,
        ui_preset=case.ui_preset.value,
        concepts=views,
        misconceptions=case.initial_misconceptions,
        user_requested_action=case.user_requested_action,
        has_active_reading_passage=case.has_active_reading_passage,
        chapter_just_ended=case.chapter_just_ended,
        key_concepts_unverified=case.key_concepts_unverified,
    )
    trace = decide(inp)
    selected = trace.selected_action
    failures: list[str] = []

    if case.expected_action.value not in (selected,):
        failures.append(f"expected {case.expected_action.value}, got {selected}")
    if case.forbidden_actions and Action(selected) in case.forbidden_actions:
        failures.append(f"selected forbidden action {selected}")
    if case.allowed_actions and Action(selected) not in case.allowed_actions:
        failures.append(f"selected {selected} not in allowed {sorted(a.value for a in case.allowed_actions)}")

    return CaseResult(
        case_id=case.case_id,
        passed=not failures,
        selected_action=selected,
        expected_action=case.expected_action.value,
        failures=failures,
    )


def run_all() -> list[CaseResult]:
    return [run_case(c) for c in GOLDEN_CASES]
