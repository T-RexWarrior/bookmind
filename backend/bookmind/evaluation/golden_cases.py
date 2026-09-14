"""GoldenCase — the replayable evaluation fixture (OPEN_SOURCE_REFERENCES.md §4,
EVALUATION.md §1).

A GoldenCase fixes: initial book/learner state, the mode, an interaction
history, a current event, the expected action, allowed/forbidden actions and
expected state changes. Comparison checks structure & invariants — never
model text.

These 10 cases exercise the decision engine and the state machine across the
Java/OOP scenario. They run without any LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import Action, ActivityMode, InterventionPolicy, Level, MisconceptionStatus, UIPreset
from ..domain.models import LearnerConceptState, LevelRecord, MisconceptionHypothesis


@dataclass
class ExpectedStateChange:
    concept_id: str
    field: str  # e.g. "current_verified_level"
    old: str
    new: str


@dataclass
class GoldenCase:
    case_id: str
    description: str
    activity_mode: ActivityMode
    intervention_policy: InterventionPolicy
    ui_preset: UIPreset
    # Initial learner states seeded into the repo before the event.
    initial_states: list[LearnerConceptState] = field(default_factory=list)
    initial_misconceptions: list[MisconceptionHypothesis] = field(default_factory=list)
    # Decision-input flags.
    chapter_just_ended: bool = False
    key_concepts_unverified: bool = False
    has_active_reading_passage: bool = False
    pending_concept_id: str | None = None
    user_requested_action: Action | None = None
    # Expectations.
    expected_action: Action = Action.WAIT
    allowed_actions: set[Action] = field(default_factory=set)
    forbidden_actions: set[Action] = field(default_factory=set)
    expected_state_changes: list[ExpectedStateChange] = field(default_factory=list)
    expected_reason_facts: list[str] = field(default_factory=list)


def _state(cid: str, l1_status="UNVERIFIED", current="L0", highest="L0", goal=0.5) -> LearnerConceptState:
    s = LearnerConceptState(project_id="p1", concept_id=cid, goal_relevance=goal)
    s.levels[Level.L1.value] = LevelRecord(status=l1_status)
    s.current_verified_level = Level(current)
    s.highest_ever_level = Level(highest)
    return s


# --------------------------------------------------------------------------
# The 10 golden cases
# --------------------------------------------------------------------------
GOLDEN_CASES: list[GoldenCase] = [
    GoldenCase(
        case_id="gc_01_quiet_reading_waits",
        description="Quiet Reading with an active passage: system must not interrupt.",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.QUIET,
        ui_preset=UIPreset.QUIET_READING,
        has_active_reading_passage=True,
        initial_states=[_state("c_reference", goal=0.9)],
        expected_action=Action.CONTINUE_READING,
        allowed_actions={Action.CONTINUE_READING, Action.WAIT},
        forbidden_actions={Action.VERIFY, Action.DIAGNOSE, Action.REMEDIATE},
        expected_reason_facts=["CONTINUE_READING"],
    ),
    GoldenCase(
        case_id="gc_02_deep_learning_chapter_end_verify",
        description="Deep Learning, chapter just ended, key concept unverified → VERIFY.",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        chapter_just_ended=True,
        key_concepts_unverified=True,
        initial_states=[_state("c_polymorphism", l1_status="UNVERIFIED", goal=0.95)],
        expected_action=Action.VERIFY,
        allowed_actions={Action.VERIFY, Action.WAIT},
        forbidden_actions={Action.ANSWER},
        expected_reason_facts=["VERIFY"],
    ),
    GoldenCase(
        case_id="gc_03_unverified_prerequisite_blocks",
        description="Pending concept c_object has unverified strong prerequisite c_reference → LEARN_PREREQUISITE.",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        pending_concept_id="c_object",
        initial_states=[_state("c_reference", l1_status="UNVERIFIED", goal=0.9), _state("c_object", goal=0.9)],
        expected_action=Action.LEARN_PREREQUISITE,
        allowed_actions={Action.LEARN_PREREQUISITE, Action.WAIT},
        expected_reason_facts=["LEARN_PREREQUISITE", "prerequisite"],
    ),
    GoldenCase(
        case_id="gc_04_expired_key_concept_review",
        description="High-goal concept EXPIRED → REVIEW (not LEARN_PREREQUISITE).",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        initial_states=[_state("c_value_equality", l1_status="EXPIRED", current="L1", highest="L1", goal=0.9)],
        expected_action=Action.REVIEW,
        allowed_actions={Action.REVIEW, Action.WAIT},
        forbidden_actions={Action.LEARN_PREREQUISITE},
        expected_reason_facts=["REVIEW"],
    ),
    GoldenCase(
        case_id="gc_05_confirmed_misconception_remediate",
        description="CONFIRMED misconception → REMEDIATE.",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        initial_states=[_state("c_reference", goal=0.9)],
        initial_misconceptions=[MisconceptionHypothesis(project_id="p1", bug_id="bug_ref_vs_object", related_concepts=["c_reference"], status=MisconceptionStatus.CONFIRMED)],
        expected_action=Action.REMEDIATE,
        allowed_actions={Action.REMEDIATE, Action.WAIT},
        forbidden_actions={Action.VERIFY},
        expected_reason_facts=["REMEDIATE", "CONFIRMED"],
    ),
    GoldenCase(
        case_id="gc_06_likely_with_competing_diagnose",
        description="LIKELY misconception with a competing hypothesis → DIAGNOSE.",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        initial_states=[_state("c_reference_equality", goal=0.85)],
        initial_misconceptions=[
            MisconceptionHypothesis(project_id="p1", bug_id="bug_eq_vs_equals", related_concepts=["c_reference_equality"], status=MisconceptionStatus.LIKELY, hypothesis_group="eq_group"),
            MisconceptionHypothesis(project_id="p1", bug_id="bug_eq_vs_equals_alt", related_concepts=["c_reference_equality"], status=MisconceptionStatus.SUSPECTED, hypothesis_group="eq_group"),
        ],
        expected_action=Action.DIAGNOSE,
        allowed_actions={Action.DIAGNOSE, Action.WAIT},
        expected_reason_facts=["DIAGNOSE", "competing"],
    ),
    GoldenCase(
        case_id="gc_07_verifying_changed_task_verify",
        description="VERIFYING misconception → VERIFY (the changed task).",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        initial_states=[_state("c_hashcode", goal=0.85)],
        initial_misconceptions=[MisconceptionHypothesis(project_id="p1", bug_id="bug_equals_no_hashcode", related_concepts=["c_hashcode"], status=MisconceptionStatus.VERIFYING)],
        expected_action=Action.VERIFY,
        allowed_actions={Action.VERIFY, Action.WAIT},
        expected_reason_facts=["VERIFY", "VERIFYING"],
    ),
    GoldenCase(
        case_id="gc_08_assessment_only_verify",
        description="Assessment mode: system may only VERIFY (never ANSWER/REMEDIATE).",
        activity_mode=ActivityMode.ASSESSMENT,
        intervention_policy=InterventionPolicy.QUIET,
        ui_preset=UIPreset.ASSESSMENT,
        initial_states=[_state("c_polymorphism", goal=0.95)],
        expected_action=Action.VERIFY,
        allowed_actions={Action.VERIFY, Action.WAIT},
        forbidden_actions={Action.ANSWER, Action.REMEDIATE, Action.DIAGNOSE, Action.REVIEW},
        expected_reason_facts=["ASSESSMENT", "VERIFY"],
    ),
    GoldenCase(
        case_id="gc_09_user_request_priority",
        description="User explicitly requests ANSWER in Deep Learning → honoured.",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        user_requested_action=Action.ANSWER,
        initial_states=[_state("c_reference", l1_status="EXPIRED", goal=0.9)],
        expected_action=Action.ANSWER,
        allowed_actions={Action.ANSWER},
        expected_reason_facts=["user"],
    ),
    GoldenCase(
        case_id="gc_10_no_positive_action_waits",
        description="Reading+PROACTIVE, nothing pending, no passage → WAIT.",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        initial_states=[_state("c_variable", l1_status="VERIFIED", current="L1", highest="L1", goal=0.5)],
        expected_action=Action.WAIT,
        allowed_actions={Action.WAIT},
        forbidden_actions={Action.VERIFY, Action.REVIEW},
        expected_reason_facts=["WAIT"],
    ),
]
