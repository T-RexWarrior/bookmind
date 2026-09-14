"""Decision gold states — EVALUATION.md §6.5 (Next Action Quality), Phase 6.

A fixed set of learner-state snapshots, each with a human-annotated expected
action, allowed/forbidden actions, and the rule that should fire. These pin the
Phase 6 acceptance criteria of ROADMAP Phase 6:

  - 能解释每条规则是否命中 (each rule's hit/miss is explainable);
  - 不会把 L0 先修误叫作复习 (an L0 prerequisite is LEARN_PREREQUISITE,
    never REVIEW);
  - Quiet 模式不过度干预 (QUIET never proactively interrupts);
  - 用户可选择直接继续 (the user may choose to continue directly — covered
    here via the user-requested-action priority rule, and end-to-end via the
    Recovery service tests).

Every case is fully deterministic — no LLM. The runner asserts that the
decision engine selects the expected action, stays within allowed_actions,
avoids forbidden_actions, and fires the expected rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import (
    Action,
    ActivityMode,
    InterventionPolicy,
    Level,
    LevelStatus,
    MisconceptionStatus,
    UIPreset,
)
from ..domain.models import (
    Concept,
    LearnerConceptState,
    LevelRecord,
    MisconceptionHypothesis,
)
from ..engine.decision.next_action import ConceptView, DecisionInput, decide


@dataclass
class DecisionGoldCase:
    case_id: str
    description: str
    activity_mode: ActivityMode
    intervention_policy: InterventionPolicy
    ui_preset: UIPreset
    concepts: list[ConceptView]
    misconceptions: list[MisconceptionHypothesis] = field(default_factory=list)
    # Decision-input flags.
    chapter_just_ended: bool = False
    key_concepts_unverified: bool = False
    has_active_reading_passage: bool = False
    user_requested_action: Action | None = None
    user_requested_concept_id: str | None = None
    # Expectations.
    expected_action: Action = Action.WAIT
    expected_rule: int = -1
    allowed_actions: set[Action] = field(default_factory=set)
    forbidden_actions: set[Action] = field(default_factory=set)
    expected_concept_id: str | None = None


def _concept(cid="c1", importance=0.5, goal=0.5, chapter="Fundamentals", prereqs=None) -> Concept:
    return Concept(
        concept_id=cid, book_id="b", name=cid, chapter=chapter,
        importance=importance, goal_relevance=goal, prerequisites=prereqs or [],
    )


def _state(cid="c1", l1=LevelStatus.UNVERIFIED, l2=LevelStatus.UNVERIFIED) -> LearnerConceptState:
    s = LearnerConceptState(project_id="p", concept_id=cid)
    s.levels[Level.L1.value] = LevelRecord(status=l1)
    s.levels[Level.L2.value] = LevelRecord(status=l2)
    return s


def _view(cid="c1", importance=0.5, goal=0.5, l1=LevelStatus.UNVERIFIED, l2=LevelStatus.UNVERIFIED,
          **kw) -> ConceptView:
    return ConceptView(concept=_concept(cid, importance, goal), state=_state(cid, l1, l2), **kw)


def _build_cases() -> list[DecisionGoldCase]:
    cases: list[DecisionGoldCase] = []

    # 1. QUIET must not proactively interrupt even when a key concept is
    #    expired — it waits instead of REVIEW/VERIFY. (Quiet 不过度干预)
    cases.append(DecisionGoldCase(
        case_id="quiet_no_interrupt_on_expired",
        description="QUIET mode must wait rather than proactively review an expired key concept",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.QUIET,
        ui_preset=UIPreset.QUIET_READING,
        concepts=[_view("c1", goal=0.9, l1=LevelStatus.EXPIRED, is_strong_prerequisite_for_pending=True)],
        expected_action=Action.WAIT,
        expected_rule=12,
        allowed_actions={Action.WAIT, Action.CONTINUE_READING},
        forbidden_actions={Action.VERIFY, Action.REVIEW, Action.REMEDIATE, Action.DIAGNOSE},
    ))

    # 2. PROACTIVE may review an expired key concept (rule 8).
    cases.append(DecisionGoldCase(
        case_id="proactive_reviews_expired_key",
        description="PROACTIVE reviews a high-goal EXPIRED concept (rule 8)",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        concepts=[_view("c1", goal=0.9, l1=LevelStatus.EXPIRED, is_strong_prerequisite_for_pending=True)],
        expected_action=Action.REVIEW,
        expected_rule=8,
        allowed_actions={Action.REVIEW, Action.WAIT, Action.CONTINUE_READING},
        forbidden_actions={Action.REMEDIATE, Action.DIAGNOSE},
    ))

    # 3. L0 prerequisite is LEARN_PREREQUISITE, NOT REVIEW — even in PROACTIVE.
    #    (不会把 L0 先修误叫作复习)
    cases.append(DecisionGoldCase(
        case_id="l0_prereq_is_learn_not_review",
        description="An unverified strong prerequisite triggers LEARN_PREREQUISITE, never REVIEW",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        concepts=[
            _view("c1", goal=0.9, l1=LevelStatus.UNVERIFIED),
            _view("c0", goal=0.3, importance=0.8, l1=LevelStatus.UNVERIFIED,
                  has_unverified_strong_prerequisite=True),
        ],
        expected_action=Action.LEARN_PREREQUISITE,
        expected_rule=7,
        expected_concept_id="c0",
        allowed_actions={Action.LEARN_PREREQUISITE, Action.WAIT, Action.CONTINUE_READING},
        forbidden_actions={Action.REVIEW, Action.VERIFY},
    ))

    # 4. CONFIRMED misconception → REMEDIATE (rule 3), not REVIEW.
    cases.append(DecisionGoldCase(
        case_id="confirmed_misconception_remediates",
        description="A CONFIRMED misconception triggers REMEDIATE (rule 3), not REVIEW",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        concepts=[_view("c1", goal=0.9, l1=LevelStatus.VERIFIED)],
        misconceptions=[MisconceptionHypothesis(
            project_id="p", bug_id="bug1", related_concepts=["c1"],
            status=MisconceptionStatus.CONFIRMED,
        )],
        expected_action=Action.REMEDIATE,
        expected_rule=3,
        allowed_actions={Action.REMEDIATE, Action.WAIT, Action.CONTINUE_READING},
        forbidden_actions={Action.REVIEW},
    ))

    # 5. User-requested ANSWER wins over everything (rule 1) — 用户可主动选择.
    cases.append(DecisionGoldCase(
        case_id="user_request_wins",
        description="A user-requested ANSWER takes priority over a would-be REVIEW (rule 1)",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        concepts=[_view("c1", goal=0.9, l1=LevelStatus.EXPIRED, is_strong_prerequisite_for_pending=True)],
        user_requested_action=Action.ANSWER,
        user_requested_concept_id="c1",
        expected_action=Action.ANSWER,
        expected_rule=1,
        allowed_actions={Action.ANSWER},
        forbidden_actions={Action.REVIEW, Action.REMEDIATE},
    ))

    # 6. User can choose to continue reading directly (rule 1 → CONTINUE_READING
    #    is a legal user action in READING+PROACTIVE).
    cases.append(DecisionGoldCase(
        case_id="user_chooses_continue_reading",
        description="A user may explicitly request CONTINUE_READING, overriding a would-be REVIEW",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        concepts=[_view("c1", goal=0.9, l1=LevelStatus.EXPIRED, is_strong_prerequisite_for_pending=True)],
        user_requested_action=Action.CONTINUE_READING,
        expected_action=Action.CONTINUE_READING,
        expected_rule=1,
        allowed_actions={Action.CONTINUE_READING},
        forbidden_actions={Action.REVIEW},
    ))

    # 7. RELAPSED with competing hypothesis → DIAGNOSE (rule 2), not REMEDIATE.
    cases.append(DecisionGoldCase(
        case_id="relapsed_with_competing_diagnoses",
        description="RELAPSED with a competing hypothesis triggers DIAGNOSE (rule 2)",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        concepts=[_view("c1", goal=0.9, l1=LevelStatus.VERIFIED)],
        misconceptions=[
            MisconceptionHypothesis(project_id="p", bug_id="bug1", related_concepts=["c1"],
                                    status=MisconceptionStatus.RELAPSED, hypothesis_group="g1"),
            MisconceptionHypothesis(project_id="p", bug_id="bug2", related_concepts=["c1"],
                                    status=MisconceptionStatus.SUSPECTED, hypothesis_group="g1"),
        ],
        expected_action=Action.DIAGNOSE,
        expected_rule=2,
        allowed_actions={Action.DIAGNOSE, Action.WAIT},
        forbidden_actions={Action.REMEDIATE, Action.REVIEW},
    ))

    # 8. ASSESSMENT → VERIFY (rule 10), ANSWER is forbidden.
    cases.append(DecisionGoldCase(
        case_id="assessment_verifies",
        description="ASSESSMENT verifies at goal level (rule 10); ANSWER is forbidden",
        activity_mode=ActivityMode.ASSESSMENT,
        intervention_policy=InterventionPolicy.QUIET,
        ui_preset=UIPreset.ASSESSMENT,
        concepts=[_view("c1", goal=0.9, l1=LevelStatus.UNVERIFIED)],
        expected_action=Action.VERIFY,
        expected_rule=10,
        allowed_actions={Action.VERIFY, Action.WAIT},
        forbidden_actions={Action.ANSWER, Action.REVIEW, Action.REMEDIATE, Action.CONTINUE_READING},
    ))

    # 9. No positive-value action in QUIET → WAIT (rule 12), and CONTINUE_READING
    #    is forbidden when there is no active passage.
    cases.append(DecisionGoldCase(
        case_id="quiet_waits_with_no_passage",
        description="QUIET READING with no active passage waits; no forced continue",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.QUIET,
        ui_preset=UIPreset.QUIET_READING,
        concepts=[_view("c1", goal=0.5, l1=LevelStatus.UNVERIFIED)],
        expected_action=Action.WAIT,
        expected_rule=12,
        allowed_actions={Action.WAIT},
        forbidden_actions={Action.VERIFY, Action.REVIEW, Action.CONTINUE_READING},
    ))

    # 10. Reading with an active passage → CONTINUE_READING (rule 11).
    cases.append(DecisionGoldCase(
        case_id="reading_with_passage_continues",
        description="READING with an active passage continues reading (rule 11)",
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.QUIET,
        ui_preset=UIPreset.QUIET_READING,
        concepts=[_view("c1", goal=0.5, l1=LevelStatus.UNVERIFIED)],
        has_active_reading_passage=True,
        expected_action=Action.CONTINUE_READING,
        expected_rule=11,
        allowed_actions={Action.CONTINUE_READING, Action.WAIT},
        forbidden_actions={Action.VERIFY, Action.REVIEW},
    ))

    return cases


GOLD_CASES: list[DecisionGoldCase] = _build_cases()


@dataclass
class GoldResult:
    case_id: str
    passed: bool
    selected_action: str
    selected_rule: int
    expected_action: str
    expected_rule: int
    failures: list[str]


def run_decision_gold() -> list[GoldResult]:
    """Run all decision gold cases; return per-case results.

    A case passes iff: the selected action equals expected_action, the firing
    rule equals expected_rule, the action is in allowed_actions, the action is
    not in forbidden_actions, and (if set) the selected concept matches.
    """
    results: list[GoldResult] = []
    for gc in GOLD_CASES:
        inp = DecisionInput(
            activity_mode=gc.activity_mode,
            intervention_policy=gc.intervention_policy,
            ui_preset=gc.ui_preset.value,
            concepts=gc.concepts,
            misconceptions=gc.misconceptions,
            chapter_just_ended=gc.chapter_just_ended,
            key_concepts_unverified=gc.key_concepts_unverified,
            has_active_reading_passage=gc.has_active_reading_passage,
            user_requested_action=gc.user_requested_action,
            user_requested_concept_id=gc.user_requested_concept_id,
        )
        trace = decide(inp)
        failures: list[str] = []
        if trace.selected_action != gc.expected_action.value:
            failures.append(
                f"action: got {trace.selected_action}, want {gc.expected_action.value}"
            )
        if trace.selected_rule != gc.expected_rule:
            failures.append(f"rule: got {trace.selected_rule}, want {gc.expected_rule}")
        if gc.allowed_actions and trace.selected_action not in {a.value for a in gc.allowed_actions}:
            failures.append(
                f"action {trace.selected_action} not in allowed {sorted(a.value for a in gc.allowed_actions)}"
            )
        if gc.forbidden_actions and trace.selected_action in {a.value for a in gc.forbidden_actions}:
            failures.append(
                f"action {trace.selected_action} is forbidden {sorted(a.value for a in gc.forbidden_actions)}"
            )
        if gc.expected_concept_id is not None and trace.selected_concept_id != gc.expected_concept_id:
            failures.append(
                f"concept: got {trace.selected_concept_id}, want {gc.expected_concept_id}"
            )
        results.append(GoldResult(
            case_id=gc.case_id,
            passed=not failures,
            selected_action=trace.selected_action,
            selected_rule=trace.selected_rule,
            expected_action=gc.expected_action.value,
            expected_rule=gc.expected_rule,
            failures=failures,
        ))
    return results


def assert_all_pass() -> None:
    """Assert every gold case passes; raise AssertionError with the failing
    case ids on the first failure."""
    results = run_decision_gold()
    failed = [r for r in results if not r.passed]
    if failed:
        msgs = [f"{r.case_id}: {'; '.join(r.failures)}" for r in failed]
        raise AssertionError("decision gold failures:\n  " + "\n  ".join(msgs))
