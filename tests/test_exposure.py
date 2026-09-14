"""L1 tests: exposure state machine — LEARNING_MODEL.md §3.

Pins the rule that exposure describes contact facts only, never a cognitive
verdict, and that READ/QUESTION/EXPLANATION evidence can move exposure but
never mastery (the Evidence Gate rejects them independently).
"""

from __future__ import annotations

from bookmind.domain.enums import ExposureState
from bookmind.domain.models import LearnerConceptState
from bookmind.engine.exposure.state import (
    ExposureEvent,
    apply_exposure,
    is_exposure_only,
)


def _state(exp=ExposureState.NONE, prog=0.0) -> LearnerConceptState:
    s = LearnerConceptState(project_id="p", concept_id="c")
    s.exposure_state = exp
    s.read_progress = prog
    return s


# --- NONE → SEEN ----------------------------------------------------------

def test_first_seen_transitions_none_to_seen():
    s = _state()
    r = apply_exposure(s, ExposureEvent(kind="seen"))
    assert r.exposure_state == ExposureState.SEEN
    assert r.changed


def test_already_seen_stays_seen():
    s = _state(exp=ExposureState.SEEN)
    r = apply_exposure(s, ExposureEvent(kind="seen"))
    assert r.exposure_state == ExposureState.SEEN
    assert not r.changed


def test_read_progress_implies_seen():
    """Advancing read progress on a NONE concept must first mark it SEEN."""
    s = _state(exp=ExposureState.NONE)
    r = apply_exposure(s, ExposureEvent(kind="read_progress", coverage=0.3))
    assert r.exposure_state == ExposureState.SEEN
    assert r.read_progress == 0.3


# --- SEEN → COMPLETED -----------------------------------------------------

def test_read_coverage_threshold_completes():
    s = _state(exp=ExposureState.SEEN, prog=0.5)
    r = apply_exposure(s, ExposureEvent(kind="read_progress", coverage=0.95),
                       read_coverage_threshold=0.9)
    assert r.exposure_state == ExposureState.COMPLETED


def test_below_threshold_stays_seen():
    s = _state(exp=ExposureState.SEEN, prog=0.5)
    r = apply_exposure(s, ExposureEvent(kind="read_progress", coverage=0.6),
                       read_coverage_threshold=0.9)
    assert r.exposure_state == ExposureState.SEEN


def test_explicit_completion_completes():
    s = _state(exp=ExposureState.SEEN, prog=0.4)
    r = apply_exposure(s, ExposureEvent(kind="completed", explicit=True))
    assert r.exposure_state == ExposureState.COMPLETED


# --- COMPLETED is sticky / progress monotonic -----------------------------

def test_completed_is_sticky():
    """Once COMPLETED, later 'seen' events never revert it."""
    s = _state(exp=ExposureState.COMPLETED, prog=1.0)
    r = apply_exposure(s, ExposureEvent(kind="seen"))
    assert r.exposure_state == ExposureState.COMPLETED
    assert not r.changed


def test_read_progress_never_decreases():
    s = _state(exp=ExposureState.SEEN, prog=0.7)
    r = apply_exposure(s, ExposureEvent(kind="read_progress", coverage=0.2))
    assert r.read_progress == 0.7  # kept the higher value


def test_read_progress_clamped_to_unit():
    s = _state(exp=ExposureState.SEEN)
    r = apply_exposure(s, ExposureEvent(kind="read_progress", coverage=1.5))
    assert r.read_progress == 1.0
    assert r.exposure_state == ExposureState.COMPLETED


# --- exposure-only types --------------------------------------------------

def test_read_question_explanation_are_exposure_only():
    assert is_exposure_only("READ")
    assert is_exposure_only("QUESTION")
    assert is_exposure_only("EXPLANATION")


def test_verify_is_not_exposure_only():
    assert not is_exposure_only("VERIFY")
    assert not is_exposure_only("CHANGED_TASK")
