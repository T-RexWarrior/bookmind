"""L1 tests: Next Best Action — LEARNING_MODEL.md §11.

Pins the rule ordering, the WAIT fallback, QUIET non-intervention, and the
lexicographic tie-break. All deterministic.
"""

from __future__ import annotations

from bookmind.domain.enums import (
    Action,
    ActivityMode,
    InterventionPolicy,
    Level,
    LevelStatus,
    MisconceptionStatus,
    UIPreset,
)
from bookmind.domain.models import Concept, LearnerConceptState, LevelRecord, MisconceptionHypothesis
from bookmind.engine.decision.next_action import ConceptView, DecisionInput, decide


def _concept(cid="c1", importance=0.5, goal=0.5, prereqs=None) -> Concept:
    return Concept(
        concept_id=cid, book_id="b", name=cid, importance=importance,
        goal_relevance=goal, prerequisites=prereqs or [],
    )


def _state(cid="c1", l1=LevelStatus.UNVERIFIED) -> LearnerConceptState:
    s = LearnerConceptState(project_id="p", concept_id=cid)
    s.levels[Level.L1.value] = LevelRecord(status=l1)
    return s


def _view(cid="c1", importance=0.5, goal=0.5, l1=LevelStatus.UNVERIFIED, **kw) -> ConceptView:
    return ConceptView(concept=_concept(cid, importance, goal), state=_state(cid, l1), **kw)


def _input(views, mis=None, **kw) -> DecisionInput:
    return DecisionInput(
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING.value,
        concepts=views,
        misconceptions=mis or [],
        **kw,
    )


# --- WAIT is the floor ---------------------------------------------------

def test_no_candidates_in_quiet_reading_waits():
    inp = DecisionInput(
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.QUIET,
        ui_preset=UIPreset.QUIET_READING.value,
        concepts=[],
        misconceptions=[],
    )
    tr = decide(inp)
    assert tr.selected_action == Action.WAIT.value
    assert tr.selected_rule == 12


def test_quiet_reading_does_not_verify_even_if_key_concept_unverified():
    """QUIET must not proactively interrupt, so it waits rather than VERIFY."""
    view = _view(goal=0.9, l1=LevelStatus.UNVERIFIED)
    inp = DecisionInput(
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.QUIET,
        ui_preset=UIPreset.QUIET_READING.value,
        concepts=[view],
        misconceptions=[],
        chapter_just_ended=True,
        key_concepts_unverified=True,
    )
    tr = decide(inp)
    # No reading passage → not CONTINUE_READING; QUIET blocks VERIFY → WAIT.
    assert tr.selected_action == Action.WAIT.value


def test_reading_with_passage_continues():
    view = _view()
    inp = _input([view], has_active_reading_passage=True)
    tr = decide(inp)
    assert tr.selected_action == Action.CONTINUE_READING.value
    assert tr.selected_rule == 11


# --- Misconception rules take priority -----------------------------------

def test_confirmed_misconception_triggers_remediate():
    view = _view("c1", goal=0.5)
    mis = MisconceptionHypothesis(project_id="p", bug_id="bug", related_concepts=["c1"], status=MisconceptionStatus.CONFIRMED)
    tr = decide(_input([view], mis=[mis]))
    assert tr.selected_action == Action.REMEDIATE.value
    assert tr.selected_rule == 3


def test_likely_with_competing_triggers_diagnose():
    view = _view("c1")
    mis = MisconceptionHypothesis(project_id="p", bug_id="bug", related_concepts=["c1"], status=MisconceptionStatus.LIKELY, hypothesis_group="g1")
    other = MisconceptionHypothesis(project_id="p", bug_id="bug2", related_concepts=["c1"], status=MisconceptionStatus.SUSPECTED, hypothesis_group="g1")
    tr = decide(_input([view], mis=[mis, other]))
    assert tr.selected_action == Action.DIAGNOSE.value
    assert tr.selected_rule == 6


def test_verified_misconception_triggers_verify_changed_task():
    view = _view("c1")
    mis = MisconceptionHypothesis(project_id="p", bug_id="bug", related_concepts=["c1"], status=MisconceptionStatus.VERIFYING)
    tr = decide(_input([view], mis=[mis]))
    assert tr.selected_action == Action.VERIFY.value
    assert tr.selected_rule == 5


# --- Prerequisite rules --------------------------------------------------

def test_unverified_strong_prerequisite_triggers_learn_prerequisite():
    pending = _view("c1", goal=0.9)
    prereq = _view("c0", goal=0.3, l1=LevelStatus.UNVERIFIED, has_unverified_strong_prerequisite=True, is_strong_prerequisite_for_pending=False)
    # c0 is the prerequisite with the flag set; c1 is the pending target.
    inp = _input([pending, prereq])
    tr = decide(inp)
    assert tr.selected_action == Action.LEARN_PREREQUISITE.value
    assert tr.selected_concept_id == "c0"
    assert tr.selected_rule == 7


def test_expired_key_concept_triggers_review():
    view = _view("c1", goal=0.9, l1=LevelStatus.EXPIRED, is_strong_prerequisite_for_pending=True)
    tr = decide(_input([view]))
    assert tr.selected_action == Action.REVIEW.value
    assert tr.selected_rule == 8


# --- Chapter-end verify in Deep Learning ---------------------------------

def test_chapter_end_key_concept_unverified_triggers_verify():
    view = _view("c1", goal=0.9, l1=LevelStatus.UNVERIFIED)
    inp = _input([view], chapter_just_ended=True, key_concepts_unverified=True)
    tr = decide(inp)
    assert tr.selected_action == Action.VERIFY.value
    assert tr.selected_rule == 9


# --- Assessment ----------------------------------------------------------

def test_assessment_always_verifies():
    view = _view("c1", goal=0.9)
    inp = DecisionInput(
        activity_mode=ActivityMode.ASSESSMENT,
        intervention_policy=InterventionPolicy.QUIET,
        ui_preset=UIPreset.ASSESSMENT.value,
        concepts=[view],
        misconceptions=[],
    )
    tr = decide(inp)
    assert tr.selected_action == Action.VERIFY.value
    assert tr.selected_rule == 10


# --- User request priority -----------------------------------------------

def test_user_request_takes_priority():
    view = _view("c1", goal=0.9, l1=LevelStatus.EXPIRED, is_strong_prerequisite_for_pending=True)
    inp = _input([view], user_requested_action=Action.ANSWER, user_requested_concept_id="c1")
    tr = decide(inp)
    assert tr.selected_action == Action.ANSWER.value
    assert tr.selected_rule == 1


def test_illegal_user_request_in_assessment_falls_through():
    """User asking for ANSWER in Assessment is illegal → system continues rules."""
    view = _view("c1", goal=0.9)
    inp = DecisionInput(
        activity_mode=ActivityMode.ASSESSMENT,
        intervention_policy=InterventionPolicy.QUIET,
        ui_preset=UIPreset.ASSESSMENT.value,
        concepts=[view],
        misconceptions=[],
        user_requested_action=Action.ANSWER,  # illegal in assessment
    )
    tr = decide(inp)
    # ANSWER illegal → falls to rule 10 (ASSESSMENT VERIFY).
    assert tr.selected_action == Action.VERIFY.value


# --- Tie-break lexicographic ---------------------------------------------

def test_tie_break_prefers_higher_goal_relevance():
    lo = _view("lo", goal=0.3, l1=LevelStatus.EXPIRED, is_strong_prerequisite_for_pending=True)
    hi = _view("hi", goal=0.9, l1=LevelStatus.EXPIRED, is_strong_prerequisite_for_pending=True)
    tr = decide(_input([lo, hi]))
    assert tr.selected_concept_id == "hi"


def test_tie_break_concept_id_is_stable_terminator():
    """When all else equal, concept_id ascending wins (stable, deterministic)."""
    b = _view("b", goal=0.9, l1=LevelStatus.EXPIRED, is_strong_prerequisite_for_pending=True)
    a = _view("a", goal=0.9, l1=LevelStatus.EXPIRED, is_strong_prerequisite_for_pending=True)
    tr = decide(_input([b, a]))
    assert tr.selected_concept_id == "a"
