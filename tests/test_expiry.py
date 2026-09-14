"""L1 tests: expiry persistence idempotency — LEARNING_MODEL.md §6.

Pins the rule that a derived VERIFIED→EXPIRED transition is persisted with an
idempotent ``expiry:{concept}:{level}:{due_at}:{policy_version}`` key, so that
re-reading state at the same instant never generates a duplicate state event.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bookmind.domain.enums import (
    EvidenceResult,
    EvidenceType,
    Level,
    LevelStatus,
)
from bookmind.domain.models import (
    AnswerJudgment,
    Book,
    Concept,
    InteractionContext,
    LearningProject,
    ProjectBook,
    ReviewPolicy,
    TrustedTaskContext,
    User,
)
from bookmind.domain.enums import ActivityMode, InterventionPolicy, JudgmentStatus, UIPreset, BookRole
from bookmind.engine.learning_engine import apply_expiry, expiry_event_key, submit_answer
from bookmind.storage.in_memory import InMemoryRepository


def _repo_with_verified_l1(as_of):
    """A project whose concept c1 has an independent L1 PASS at ``as_of``."""
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="Java"))
    repo.add_book(Book(book_id="b1", owner_user_id="u1", source_hash="h", title="Java"))
    repo.link_book(ProjectBook(project_id="p1", book_id="b1", role=BookRole.PRIMARY))
    repo.add_concept(Concept(concept_id="c1", book_id="b1", name="c1", importance=0.8, goal_relevance=0.8))
    task = TrustedTaskContext(
        task_id="t1", task_version=1, target_concept_ids=["c1"],
        evidence_for_levels=[Level.L1], rubric=["recalls"],
    )
    interaction = InteractionContext(
        activity_mode=ActivityMode.READING, intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
    )
    submit_answer(
        repo, learner_id="u1", project_id="p1", task=task, interaction=interaction,
        judgment=AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.PASS),
        answer_text="a", policy=ReviewPolicy(), submission_id="s1",
        evidence_id="e1", source_book_id="b1",
    )
    # Force the verification timestamp to as_of for deterministic expiry maths.
    state = repo.get_state("p1", "c1")
    rec = state.level_record(Level.L1)
    state.set_level_record(Level.L1, rec.model_copy(update={"verified_at": as_of, "stability_days": 2.0}))
    repo.save_state(state)
    return repo


def test_expiry_event_key_is_deterministic():
    t = datetime(2025, 1, 1, tzinfo=timezone.utc)
    k1 = expiry_event_key("c1", Level.L1, t, 1)
    k2 = expiry_event_key("c1", Level.L1, t, 1)
    assert k1 == k2


def test_expiry_event_key_differs_by_level_or_policy_or_due():
    t = datetime(2025, 1, 1, tzinfo=timezone.utc)
    base = expiry_event_key("c1", Level.L1, t, 1)
    assert expiry_event_key("c1", Level.L2, t, 1) != base
    assert expiry_event_key("c1", Level.L1, t + timedelta(days=1), 1) != base
    assert expiry_event_key("c1", Level.L1, t, 2) != base


def test_apply_expiry_records_transition_when_lapsed():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    repo = _repo_with_verified_l1(t0)
    far = t0 + timedelta(days=30)  # S=2 → long expired
    res = apply_expiry(repo, project_id="p1", concept_id="c1", level=Level.L1,
                       as_of=far, policy=ReviewPolicy())
    assert res.expired
    assert res.transition_recorded
    state = repo.get_state("p1", "c1")
    assert state.level_record(Level.L1).status == LevelStatus.EXPIRED


def test_apply_expiry_is_idempotent_on_repeat():
    """Re-calling apply_expiry at the same as_of must NOT record again."""
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    repo = _repo_with_verified_l1(t0)
    far = t0 + timedelta(days=30)
    r1 = apply_expiry(repo, project_id="p1", concept_id="c1", level=Level.L1,
                      as_of=far, policy=ReviewPolicy())
    transitions_before = len(repo.transitions)
    r2 = apply_expiry(repo, project_id="p1", concept_id="c1", level=Level.L1,
                      as_of=far, policy=ReviewPolicy())
    assert r1.transition_recorded
    assert not r2.transition_recorded
    assert r2.reason.startswith("expiry already recorded")
    assert len(repo.transitions) == transitions_before  # no new transition


def test_apply_expiry_noop_when_not_yet_expired():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    repo = _repo_with_verified_l1(t0)
    soon = t0 + timedelta(days=1)  # R still well above 0.9
    res = apply_expiry(repo, project_id="p1", concept_id="c1", level=Level.L1,
                       as_of=soon, policy=ReviewPolicy())
    assert not res.expired
    assert not res.transition_recorded


def test_apply_expiry_noop_for_unverified_level():
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    repo = _repo_with_verified_l1(t0)
    # L2 was never verified.
    res = apply_expiry(repo, project_id="p1", concept_id="c1", level=Level.L2,
                       as_of=t0 + timedelta(days=30), policy=ReviewPolicy())
    assert not res.transition_recorded
