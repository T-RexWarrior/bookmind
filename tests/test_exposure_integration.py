"""L1 integration tests: record_exposure closed loop — LEARNING_MODEL §3, §4.

Pins that READ/QUESTION/EXPLANATION evidence moves exposure but never mastery,
that exposure evidence is append-only + idempotent, and that exposure scope is
enforced just like verify evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bookmind.domain.enums import (
    EvidenceType,
    ExposureState,
    Level,
    LevelStatus,
)
from bookmind.domain.models import Book, Concept, LearningProject, ProjectBook, User
from bookmind.domain.enums import BookRole
from bookmind.engine.learning_engine import record_exposure
from bookmind.storage.in_memory import InMemoryRepository, ScopeError


def _repo(concept_id="c1", book_id="b1"):
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="Java"))
    repo.add_book(Book(book_id=book_id, owner_user_id="u1", source_hash="h", title="Java"))
    repo.link_book(ProjectBook(project_id="p1", book_id=book_id, role=BookRole.PRIMARY))
    repo.add_concept(Concept(concept_id=concept_id, book_id=book_id, name=concept_id))
    return repo


def test_read_exposure_transitions_none_to_seen():
    repo = _repo()
    res = record_exposure(
        repo, learner_id="u1", project_id="p1", concept_id="c1", source_book_id="b1",
        evidence_id="ex1", evidence_type=EvidenceType.READ,
        occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert res.changed
    assert res.exposure_state == ExposureState.SEEN
    state = repo.get_state("p1", "c1")
    assert state.exposure_state == ExposureState.SEEN
    assert state.current_verified_level == Level.L0  # never mastery


def test_exposure_never_verifies_mastery():
    """Repeated exposure must not raise any level — PRODUCT_SPEC §8."""
    repo = _repo()
    t = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        record_exposure(
            repo, learner_id="u1", project_id="p1", concept_id="c1", source_book_id="b1",
            evidence_id=f"ex{i}", evidence_type=EvidenceType.READ, occurred_at=t,
        )
    state = repo.get_state("p1", "c1")
    assert state.current_verified_level == Level.L0
    assert all(state.level_record(l).status == LevelStatus.UNVERIFIED
               for l in (Level.L1, Level.L2, Level.L3, Level.L4))


def test_read_coverage_completes():
    repo = _repo()
    t = datetime(2025, 1, 1, tzinfo=timezone.utc)
    record_exposure(
        repo, learner_id="u1", project_id="p1", concept_id="c1", source_book_id="b1",
        evidence_id="ex1", evidence_type=EvidenceType.READ, occurred_at=t, read_coverage=0.95,
    )
    assert repo.get_state("p1", "c1").exposure_state == ExposureState.COMPLETED


def test_exposure_is_idempotent():
    """Same evidence_id → event_key replay → no duplicate write."""
    repo = _repo()
    t = datetime(2025, 1, 1, tzinfo=timezone.utc)
    record_exposure(
        repo, learner_id="u1", project_id="p1", concept_id="c1", source_book_id="b1",
        evidence_id="ex1", evidence_type=EvidenceType.READ, occurred_at=t,
    )
    res2 = record_exposure(
        repo, learner_id="u1", project_id="p1", concept_id="c1", source_book_id="b1",
        evidence_id="ex1", evidence_type=EvidenceType.READ, occurred_at=t,
    )
    assert not res2.changed
    assert len(repo.evidence) == 1


def test_exposure_rejects_non_exposure_type():
    repo = _repo()
    with pytest.raises(ValueError):
        record_exposure(
            repo, learner_id="u1", project_id="p1", concept_id="c1", source_book_id="b1",
            evidence_id="ex1", evidence_type=EvidenceType.VERIFY,
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )


def test_exposure_scope_enforced():
    """Cannot record exposure for a concept outside the project's books."""
    repo = _repo(concept_id="c1", book_id="b1")
    # c2 belongs to b2, not linked to p1.
    repo.add_book(Book(book_id="b2", owner_user_id="u1", source_hash="h2", title="Other"))
    repo.add_concept(Concept(concept_id="c2", book_id="b2", name="c2"))
    with pytest.raises(ScopeError):
        record_exposure(
            repo, learner_id="u1", project_id="p1", concept_id="c2", source_book_id="b2",
            evidence_id="ex1", evidence_type=EvidenceType.READ,
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )


def test_exposure_wrong_learner_rejected():
    repo = _repo()
    repo.add_user(User(user_id="u2"))
    with pytest.raises(ScopeError):
        record_exposure(
            repo, learner_id="u2", project_id="p1", concept_id="c1", source_book_id="b1",
            evidence_id="ex1", evidence_type=EvidenceType.READ,
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
