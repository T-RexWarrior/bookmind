"""L1 tests: action matrix — LEARNING_MODEL.md §10.

The matrix is a fixed lookup. These tests pin the legal/illegal action sets
for every UI preset and source, including Assessment's forced QUIET and the
QUIET/PROACTIVE distinction.
"""

from __future__ import annotations

from bookmind.domain.enums import Action, ActivityMode, InterventionPolicy, UIPreset
from bookmind.engine.action_matrix import (
    dimensions_for,
    is_legal_system_action,
    is_legal_user_action,
    legal_system_actions,
    legal_user_actions,
)


def test_preset_dimensions():
    assert dimensions_for(UIPreset.QUIET_READING) == (ActivityMode.READING, InterventionPolicy.QUIET)
    assert dimensions_for(UIPreset.DEEP_LEARNING) == (ActivityMode.READING, InterventionPolicy.PROACTIVE)
    assert dimensions_for(UIPreset.REVIEW) == (ActivityMode.REVIEW, InterventionPolicy.PROACTIVE)
    assert dimensions_for(UIPreset.ASSESSMENT) == (ActivityMode.ASSESSMENT, InterventionPolicy.QUIET)


def test_quiet_reading_system_only_continues_or_waits():
    actions = legal_system_actions(ActivityMode.READING, InterventionPolicy.QUIET)
    assert actions == frozenset({Action.CONTINUE_READING, Action.WAIT})


def test_quiet_reading_blocks_system_verify():
    assert not is_legal_system_action(Action.VERIFY, ActivityMode.READING, InterventionPolicy.QUIET)
    assert not is_legal_system_action(Action.DIAGNOSE, ActivityMode.READING, InterventionPolicy.QUIET)


def test_quiet_reading_user_can_still_request_help():
    """QUIET only blocks *system* interruptions; the user may ask."""
    assert is_legal_user_action(Action.ANSWER, ActivityMode.READING, InterventionPolicy.QUIET)
    assert is_legal_user_action(Action.VERIFY, ActivityMode.READING, InterventionPolicy.QUIET)


def test_deep_learning_system_can_verify_diagnose_remediate():
    actions = legal_system_actions(ActivityMode.READING, InterventionPolicy.PROACTIVE)
    for a in (Action.VERIFY, Action.DIAGNOSE, Action.LEARN_PREREQUISITE, Action.REVIEW, Action.REMEDIATE, Action.CONTINUE_READING, Action.WAIT):
        assert a in actions


def test_review_mode_system_cannot_continue_reading():
    actions = legal_system_actions(ActivityMode.REVIEW, InterventionPolicy.PROACTIVE)
    assert Action.CONTINUE_READING not in actions
    assert Action.REVIEW in actions


def test_assessment_forced_quiet_only_verify_or_wait():
    actions = legal_system_actions(ActivityMode.ASSESSMENT, InterventionPolicy.QUIET)
    assert actions == frozenset({Action.VERIFY, Action.WAIT})


def test_assessment_user_cannot_answer():
    """Assessment forbids ANSWER; user can only submit answers / exit."""
    user = legal_user_actions(ActivityMode.ASSESSMENT, InterventionPolicy.QUIET)
    assert Action.ANSWER not in user
    assert Action.VERIFY in user


def test_assessment_user_cannot_request_hint_or_remediate():
    user = legal_user_actions(ActivityMode.ASSESSMENT, InterventionPolicy.QUIET)
    assert Action.REMEDIATE not in user
    assert Action.DIAGNOSE not in user
