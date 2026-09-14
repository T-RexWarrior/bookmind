"""L1 tests: Long-term Recovery — LEARNING_MODEL.md §12, ARCHITECTURE §12.

Pins the §12 invariants:
  - recovery scans high-value concepts (goal_relevance / importance / strong prereq);
  - it finds EXPIRED / UNSTABLE and ranks by the §11 lexicographic key;
  - it recommends a 3-minute check after a long absence, else "continue";
  - the user's choice wins (§12.5); the system never forces a check;
  - recovery does NOT build a parallel state machine — it recommends a normal
    VERIFY that goes through the existing Evidence flow.
  - a L0 prerequisite is never mislabelled as "review" (§6 acceptance:
    "不会把 L0 先修误叫作复习").
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bookmind.domain.enums import (
    Action,
    Difficulty,
    Level,
    LevelStatus,
)
from bookmind.domain.models import (
    Book,
    Concept,
    LearningProject,
    LearnerConceptState,
    LevelRecord,
    ProjectBook,
    ReviewPolicy,
    User,
)
from bookmind.domain.enums import BookRole
from bookmind.services.recovery import (
    RecoveryService,
    project_recovery_plan,
)
from bookmind.storage.in_memory import InMemoryRepository


def _utc(days_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def _seed_repo_with_concepts(*concepts: Concept, project_id="p", learner_id="u", book_id="b"):
    repo = InMemoryRepository()
    repo.add_user(User(user_id=learner_id))
    repo.create_project(LearningProject(project_id=project_id, learner_id=learner_id, name="t"))
    repo.add_book(Book(book_id=book_id, owner_user_id=learner_id, source_hash=book_id, title="T"))
    repo.link_book(ProjectBook(project_id=project_id, book_id=book_id, role=BookRole.PRIMARY))
    for c in concepts:
        repo.add_concept(c)
    return repo


def _concept(cid="c1", importance=0.5, goal=0.5, chapter="Fundamentals", prereqs=None) -> Concept:
    return Concept(
        concept_id=cid, book_id="b", name=cid, chapter=chapter,
        importance=importance, goal_relevance=goal, prerequisites=prereqs or [],
        difficulty=Difficulty.MEDIUM,
    )


def _verify_state(repo, concept_id, level=Level.L1, *, project_id="p", verified_days_ago=30.0,
                  stability_days=2.0):
    """Put a concept's level into VERIFIED with a known timestamp/stability."""
    state = repo.get_state(project_id, concept_id)
    state.set_level_record(level, LevelRecord(
        status=LevelStatus.VERIFIED,
        verified_at=_utc(verified_days_ago),
        stability_days=stability_days,
    ))
    state.current_verified_level = level
    state.highest_ever_level = level
    repo.save_state(state)
    return state


# --- scanning & candidate selection -----------------------------------------

def test_recovery_finds_expired_high_value_concept():
    c = _concept("c_poly", goal=0.95, importance=0.9, chapter="Inheritance")
    repo = _seed_repo_with_concepts(c)
    _verify_state(repo, "c_poly", Level.L2, verified_days_ago=60.0, stability_days=2.0)
    # 60 days >> 2-day stability → EXPIRED.
    plan = project_recovery_plan(repo, "p", as_of=datetime.now(timezone.utc),
                                 last_active_at=_utc(60.0))
    assert len(plan.candidates) == 1
    cand = plan.candidates[0]
    assert cand.concept_id == "c_poly"
    assert cand.reason == "EXPIRED"
    assert cand.goal_relevance == 0.95


def test_recovery_ignores_low_value_concept_even_if_expired():
    c = _concept("c_minor", goal=0.2, importance=0.2, prereqs=[])
    repo = _seed_repo_with_concepts(c)
    _verify_state(repo, "c_minor", Level.L1, verified_days_ago=60.0, stability_days=2.0)
    plan = project_recovery_plan(repo, "p", as_of=datetime.now(timezone.utc),
                                 last_active_at=_utc(60.0))
    assert plan.candidates == []


def test_recovery_finds_unstable_concept():
    c = _concept("c_inh", goal=0.9, importance=0.9)
    repo = _seed_repo_with_concepts(c)
    state = repo.get_state("p", "c_inh")
    state.set_level_record(Level.L2, LevelRecord(
        status=LevelStatus.UNSTABLE,
        verified_at=_utc(10.0),
        stability_days=4.0,
    ))
    state.current_verified_level = Level.L2
    state.highest_ever_level = Level.L2
    repo.save_state(state)
    plan = project_recovery_plan(repo, "p", as_of=datetime.now(timezone.utc),
                                 last_active_at=_utc(30.0))
    assert len(plan.candidates) == 1
    assert plan.candidates[0].reason == "UNSTABLE"


def test_recovery_does_not_mislabel_l0_prerequisite_as_review():
    """§6 acceptance: 不会把 L0 先修误叫作复习. An L0 (UNVERIFIED) strong
    prerequisite is a LEARN_PREREQUISITE target, not a REVIEW/recovery target.
    Recovery must skip fully-UNVERIFIED concepts — they have nothing to
    re-verify, only to learn."""
    prereq = _concept("c_var", goal=0.9, importance=0.9, prereqs=[])
    repo = _seed_repo_with_concepts(prereq)
    # Leave L0 — never verified.
    plan = project_recovery_plan(repo, "p", as_of=datetime.now(timezone.utc),
                                 last_active_at=_utc(60.0))
    # Nothing EXPIRED/UNSTABLE (UNVERIFIED is neither) → no candidates.
    assert plan.candidates == []
    assert plan.recommendation == "continue"


# --- ranking (§11 lexicographic key) ----------------------------------------

def test_recovery_ranks_by_goal_relevance_desc():
    lo = _concept("c_lo", goal=0.7, importance=0.7, chapter="x")
    hi = _concept("c_hi", goal=0.95, importance=0.7, chapter="y")
    repo = _seed_repo_with_concepts(lo, hi)
    _verify_state(repo, "c_lo", Level.L1, verified_days_ago=60.0, stability_days=2.0)
    _verify_state(repo, "c_hi", Level.L1, verified_days_ago=60.0, stability_days=2.0)
    plan = project_recovery_plan(repo, "p", as_of=datetime.now(timezone.utc),
                                 last_active_at=_utc(60.0))
    assert plan.candidates[0].concept_id == "c_hi"
    assert plan.candidates[1].concept_id == "c_lo"


def test_recovery_tiebreak_concept_id_ascending_when_equal():
    a = _concept("c_aaa", goal=0.9, importance=0.9)
    b = _concept("c_bbb", goal=0.9, importance=0.9)
    repo = _seed_repo_with_concepts(b, a)  # insert in arbitrary order
    _verify_state(repo, "c_aaa", Level.L1, verified_days_ago=60.0, stability_days=2.0)
    _verify_state(repo, "c_bbb", Level.L1, verified_days_ago=60.0, stability_days=2.0)
    plan = project_recovery_plan(repo, "p", as_of=datetime.now(timezone.utc),
                                 last_active_at=_utc(60.0))
    assert plan.candidates[0].concept_id == "c_aaa"
    assert plan.candidates[1].concept_id == "c_bbb"


# --- recommendation logic ---------------------------------------------------

def test_recommends_recovery_check_after_long_absence_with_expired():
    c = _concept("c_poly", goal=0.95, importance=0.9)
    repo = _seed_repo_with_concepts(c)
    _verify_state(repo, "c_poly", Level.L2, verified_days_ago=60.0, stability_days=2.0)
    plan = project_recovery_plan(repo, "p", as_of=datetime.now(timezone.utc),
                                 last_active_at=_utc(60.0))
    assert plan.recommendation == "recovery_check"
    assert "3-minute" in plan.rationale or "recovery check" in plan.rationale


def test_recommends_continue_when_absence_is_short():
    c = _concept("c_poly", goal=0.95, importance=0.9)
    repo = _seed_repo_with_concepts(c)
    _verify_state(repo, "c_poly", Level.L2, verified_days_ago=1.0, stability_days=2.0)
    # Only 1 day away → mild decay, recommend continue.
    plan = project_recovery_plan(repo, "p", as_of=datetime.now(timezone.utc),
                                 last_active_at=_utc(1.0))
    assert plan.recommendation == "continue"


def test_recommends_continue_when_no_candidates():
    c = _concept("c_minor", goal=0.2, importance=0.2)
    repo = _seed_repo_with_concepts(c)
    plan = project_recovery_plan(repo, "p", as_of=datetime.now(timezone.utc),
                                 last_active_at=_utc(60.0))
    assert plan.recommendation == "continue"
    assert plan.candidates == []


# --- user choice wins (§12.5) -----------------------------------------------

def test_user_choice_continue_overrides_recovery_recommendation():
    c = _concept("c_poly", goal=0.95, importance=0.9)
    repo = _seed_repo_with_concepts(c)
    _verify_state(repo, "c_poly", Level.L2, verified_days_ago=60.0, stability_days=2.0)
    svc = RecoveryService(repo)
    plan = svc.build_plan(project_id="p", as_of=datetime.now(timezone.utc),
                          last_active_at=_utc(60.0))
    assert plan.recommendation == "recovery_check"
    # User says "continue" — that wins.
    svc.record_choice(plan, "continue")
    action, target = svc.recommend_action(plan)
    assert action == Action.CONTINUE_READING
    assert target is None


def test_user_choice_recovery_check_picks_top_candidate():
    c = _concept("c_poly", goal=0.95, importance=0.9)
    repo = _seed_repo_with_concepts(c)
    _verify_state(repo, "c_poly", Level.L2, verified_days_ago=60.0, stability_days=2.0)
    svc = RecoveryService(repo)
    plan = svc.build_plan(project_id="p", as_of=datetime.now(timezone.utc),
                          last_active_at=_utc(60.0))
    svc.record_choice(plan, "recovery_check")
    action, target = svc.recommend_action(plan)
    assert action == Action.VERIFY
    assert target == "c_poly"


def test_invalid_choice_rejected():
    c = _concept("c_poly", goal=0.95, importance=0.9)
    repo = _seed_repo_with_concepts(c)
    svc = RecoveryService(repo)
    plan = svc.build_plan(project_id="p", as_of=datetime.now(timezone.utc),
                          last_active_at=_utc(60.0))
    try:
        svc.record_choice(plan, "something_else")
        assert False, "should have raised"
    except ValueError:
        pass


def test_recommend_action_respects_continue_when_no_candidates():
    repo = _seed_repo_with_concepts(_concept("c_minor", goal=0.2, importance=0.2))
    svc = RecoveryService(repo)
    plan = svc.build_plan(project_id="p", as_of=datetime.now(timezone.utc),
                          last_active_at=_utc(60.0))
    # No candidates → even "recovery_check" choice can't VERIFY nothing.
    svc.record_choice(plan, "recovery_check")
    action, target = svc.recommend_action(plan)
    assert target is None


# --- no parallel state machine ----------------------------------------------

def test_recovery_produces_normal_verify_action_not_a_special_one():
    """§12.6: recovery tasks produce normal Evidence, not another state system.
    The recommended action is a plain VERIFY that flows through the normal
    submit_answer transaction."""
    c = _concept("c_poly", goal=0.95, importance=0.9)
    repo = _seed_repo_with_concepts(c)
    _verify_state(repo, "c_poly", Level.L2, verified_days_ago=60.0, stability_days=2.0)
    svc = RecoveryService(repo)
    plan = svc.build_plan(project_id="p", as_of=datetime.now(timezone.utc),
                          last_active_at=_utc(60.0))
    svc.record_choice(plan, "recovery_check")
    action, _ = svc.recommend_action(plan)
    assert action == Action.VERIFY  # the normal verify action, nothing new


def test_days_away_derived_from_evidence_when_last_active_unknown():
    c = _concept("c_poly", goal=0.95, importance=0.9)
    repo = _seed_repo_with_concepts(c)
    _verify_state(repo, "c_poly", Level.L2, verified_days_ago=60.0, stability_days=2.0)
    # Write an evidence-like entry 60 days ago by appending directly.
    from bookmind.domain.enums import EvidenceResult, EvidenceType, HintLevel
    from bookmind.domain.models import Evidence
    repo.append_evidence(Evidence(
        evidence_id="e1", event_key="k1", project_id="p", concept_id="c_poly",
        source_book_id="b", evidence_type=EvidenceType.VERIFY,
        required_level=Level.L2, result=EvidenceResult.PASS, independent=True,
        hint_level=HintLevel.NONE, task_id="t1", occurred_at=_utc(60.0),
    ))
    plan = project_recovery_plan(repo, "p", as_of=datetime.now(timezone.utc))
    # days_away derived from the evidence timestamp ≈ 60.
    assert plan.days_away >= 59.0
