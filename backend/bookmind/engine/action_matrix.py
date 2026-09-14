"""Learning-mode action matrix — LEARNING_MODEL.md §10.

This is the *single* place that decides which actions are legal for a given
``(activity_mode, intervention_policy)`` and source (system-proactive vs
user-requested). Every other module references ``legal_actions`` instead of
re-deriving the matrix.
"""

from __future__ import annotations

from ..domain.enums import Action, ActivityMode, InterventionPolicy, UIPreset


# The four UI presets map to exactly these underlying dimension pairs.
UI_PRESET_DIMENSIONS: dict[UIPreset, tuple[ActivityMode, InterventionPolicy]] = {
    UIPreset.QUIET_READING: (ActivityMode.READING, InterventionPolicy.QUIET),
    UIPreset.DEEP_LEARNING: (ActivityMode.READING, InterventionPolicy.PROACTIVE),
    UIPreset.REVIEW: (ActivityMode.REVIEW, InterventionPolicy.PROACTIVE),
    UIPreset.ASSESSMENT: (ActivityMode.ASSESSMENT, InterventionPolicy.QUIET),  # forced QUIET
}


def dimensions_for(preset: UIPreset) -> tuple[ActivityMode, InterventionPolicy]:
    return UI_PRESET_DIMENSIONS[preset]


# System-proactive actions per (activity_mode, intervention_policy).
# QUIET blocks *system-initiated* interruptions; the user may still ask for
# help explicitly. Assessment is forced QUIET and additionally forbids
# ANSWER / hint / textbook display.
_SYSTEM_ACTIONS: dict[tuple[ActivityMode, InterventionPolicy], frozenset[Action]] = {
    (ActivityMode.READING, InterventionPolicy.QUIET): frozenset(
        {Action.CONTINUE_READING, Action.WAIT}
    ),
    (ActivityMode.READING, InterventionPolicy.PROACTIVE): frozenset(
        {
            Action.CONTINUE_READING,
            Action.VERIFY,
            Action.DIAGNOSE,
            Action.LEARN_PREREQUISITE,
            Action.REVIEW,
            Action.REMEDIATE,
            Action.WAIT,
        }
    ),
    (ActivityMode.REVIEW, InterventionPolicy.PROACTIVE): frozenset(
        {
            Action.REVIEW,
            Action.VERIFY,
            Action.DIAGNOSE,
            Action.LEARN_PREREQUISITE,
            Action.REMEDIATE,
            Action.WAIT,
        }
    ),
    # Assessment: system may only VERIFY (independent tasks) or WAIT.
    (ActivityMode.ASSESSMENT, InterventionPolicy.QUIET): frozenset({Action.VERIFY, Action.WAIT}),
}


# User-requested actions. QUIET never blocks a user who *asks*; Assessment
# forbids ANSWER (the user can only submit answers / exit).
_USER_ACTIONS: dict[tuple[ActivityMode, InterventionPolicy], frozenset[Action]] = {
    (ActivityMode.READING, InterventionPolicy.QUIET): frozenset(
        {
            Action.ANSWER,
            Action.VERIFY,
            Action.DIAGNOSE,
            Action.LEARN_PREREQUISITE,
            Action.REVIEW,
            Action.REMEDIATE,
            Action.CONTINUE_READING,
        }
    ),
    (ActivityMode.READING, InterventionPolicy.PROACTIVE): frozenset(
        {
            Action.ANSWER,
            Action.VERIFY,
            Action.DIAGNOSE,
            Action.LEARN_PREREQUISITE,
            Action.REVIEW,
            Action.REMEDIATE,
            Action.CONTINUE_READING,
        }
    ),
    (ActivityMode.REVIEW, InterventionPolicy.PROACTIVE): frozenset(
        {
            Action.ANSWER,
            Action.VERIFY,
            Action.DIAGNOSE,
            Action.LEARN_PREREQUISITE,
            Action.REMEDIATE,
            Action.REVIEW,
        }
    ),
    (ActivityMode.ASSESSMENT, InterventionPolicy.QUIET): frozenset({Action.VERIFY}),
}


def legal_system_actions(
    activity_mode: ActivityMode,
    intervention_policy: InterventionPolicy,
) -> frozenset[Action]:
    """Actions the *system* may proactively take."""
    return _SYSTEM_ACTIONS[(activity_mode, intervention_policy)]


def legal_user_actions(
    activity_mode: ActivityMode,
    intervention_policy: InterventionPolicy,
) -> frozenset[Action]:
    """Actions a *user* may explicitly request."""
    return _USER_ACTIONS[(activity_mode, intervention_policy)]


def is_legal_system_action(
    action: Action,
    activity_mode: ActivityMode,
    intervention_policy: InterventionPolicy,
) -> bool:
    return action in legal_system_actions(activity_mode, intervention_policy)


def is_legal_user_action(
    action: Action,
    activity_mode: ActivityMode,
    intervention_policy: InterventionPolicy,
) -> bool:
    return action in legal_user_actions(activity_mode, intervention_policy)


def is_assessment(activity_mode: ActivityMode) -> bool:
    return activity_mode == ActivityMode.ASSESSMENT
