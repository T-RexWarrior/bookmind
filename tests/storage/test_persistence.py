"""M1 restart-survival test — proves persistence survives a process restart
(PRODUCTIZATION M1 completion criterion).

Uses a temp SQLite file, writes state through the SqlRepository, drops the
instance (simulating a restart), opens a fresh one on the same file, and
asserts the project / concepts / state / evidence are all still there.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from bookmind.domain.enums import BookRole, EvidenceResult, EvidenceType, Level
from bookmind.domain.models import (
    Book,
    Concept,
    Evidence,
    LearningProject,
    ProjectBook,
    User,
)
from bookmind.storage.sql import SqlRepository


@pytest.fixture()
def sqlite_path(tmp_path):
    return str(tmp_path / "bookmind.db")


def _new_repo(path):
    r = SqlRepository(f"sqlite:///{path}")
    r.create_schema()
    return r


def test_state_survives_restart(sqlite_path):
    # --- first "process" ---
    repo = _new_repo(sqlite_path)
    repo.add_user(User(user_id="u1", display_name="Ada"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="Java"))
    repo.add_book(Book(book_id="b1", owner_user_id="u1", source_hash="b1", title="Java Core"))
    repo.link_book(ProjectBook(project_id="p1", book_id="b1", role=BookRole.PRIMARY))
    repo.add_concept(Concept(concept_id="c_reference", book_id="b1", name="Reference"))

    # Verify L1 → writes evidence + state.
    repo.append_evidence(Evidence(
        evidence_id="e1", event_key="k1", project_id="p1", concept_id="c_reference",
        source_book_id="b1", evidence_type=EvidenceType.VERIFY,
        required_level=Level.L1, result=EvidenceResult.PASS, independent=True,
    ))
    state = repo.get_state("p1", "c_reference")
    state.current_verified_level = Level.L1
    state.bump_version()
    repo.save_state(state)

    # --- simulate restart: drop the instance, reopen the same file ---
    del repo
    repo2 = _new_repo(sqlite_path)

    # Project, book, concept survive.
    proj = repo2.assert_project_owned_by("p1", "u1")
    assert proj.name == "Java"
    assert repo2.allowed_book_ids("p1") == {"b1"}
    concepts = repo2.concepts_for_book("b1")
    assert any(c.concept_id == "c_reference" for c in concepts)

    # Evidence survives (idempotent key still blocks re-write).
    ev = repo2.evidence_for("p1", "c_reference")
    assert len(ev) == 1
    assert ev[0].result == EvidenceResult.PASS
    assert repo2.append_evidence(Evidence(
        evidence_id="e2", event_key="k1", project_id="p1", concept_id="c_reference",
        source_book_id="b1", evidence_type=EvidenceType.VERIFY,
        required_level=Level.L1, result=EvidenceResult.PASS, independent=True,
    )) is False  # same event_key → idempotent no-op

    # State survives.
    state2 = repo2.get_state("p1", "c_reference")
    assert state2.current_verified_level == Level.L1
    assert state2.version == 1


def test_two_users_two_projects_isolated_on_disk(sqlite_path):
    repo = _new_repo(sqlite_path)
    for uid, pid in (("u1", "p1"), ("u2", "p2")):
        repo.add_user(User(user_id=uid))
        repo.create_project(LearningProject(project_id=pid, learner_id=uid, name=pid))
        repo.add_book(Book(book_id=f"b_{pid}", owner_user_id=uid, source_hash=pid, title=pid))
        repo.link_book(ProjectBook(project_id=pid, book_id=f"b_{pid}", role=BookRole.PRIMARY))
        repo.add_concept(Concept(concept_id="c_shared", book_id=f"b_{pid}", name="shared"))
        s = repo.get_state(pid, "c_shared")
        s.current_verified_level = Level.L2 if uid == "u1" else Level.L0
        s.bump_version()
        repo.save_state(s)

    # u1 cannot access p2.
    with pytest.raises(Exception):
        repo.assert_project_owned_by("p2", "u1")
    # p1's state is L2, p2's is L0 — no cross-contamination.
    assert repo.get_state("p1", "c_shared").current_verified_level == Level.L2
    assert repo.get_state("p2", "c_shared").current_verified_level == Level.L0
