"""Repository contract tests — both implementations must behave identically
(PRODUCTIZATION M1, ARCHITECTURE §9).

The deterministic Engine depends on the ``Repository`` protocol; any concrete
impl that claims to satisfy it must pass these behavior tests. Parametrized
over ``InMemoryRepository`` and ``SqlRepository(sqlite:///:memory:)`` so the
SQLite path is held to the same contract as the 409-test in-memory path.
"""

from __future__ import annotations

import pytest

from bookmind.domain.enums import BookRole, EvidenceResult, EvidenceType, Level
from bookmind.domain.models import (
    Book,
    Concept,
    Evidence,
    LearnerConceptState,
    LearningProject,
    MisconceptionHypothesis,
    MisconceptionSignal,
    ProjectBook,
    StateTransition,
    User,
)
from bookmind.domain.source_ref import SourceRef
from bookmind.storage.in_memory import InMemoryRepository, ScopeError
from bookmind.storage.sql import SqlRepository


# --- fixtures -------------------------------------------------------------

@pytest.fixture(params=["memory", "sqlite"], ids=["memory", "sqlite"])
def repo(request):
    if request.param == "memory":
        return InMemoryRepository()
    r = SqlRepository("sqlite:///:memory:")
    r.create_schema()
    return r


def _seed_user_project(repo, user_id="u1", project_id="p1"):
    repo.add_user(User(user_id=user_id, display_name=user_id))
    repo.create_project(LearningProject(
        project_id=project_id, learner_id=user_id, name="Java",
    ))


def _seed_book(repo, project_id="p1", book_id="b1", learner_id="u1"):
    repo.add_book(Book(book_id=book_id, owner_user_id=learner_id,
                       source_hash=book_id, title="Java Core"))
    repo.link_book(ProjectBook(project_id=project_id, book_id=book_id, role=BookRole.PRIMARY))


# --- identity / scoping ---------------------------------------------------

def test_create_project_requires_existing_learner(repo):
    with pytest.raises(ScopeError):
        repo.create_project(LearningProject(project_id="p1", learner_id="ghost", name="x"))


def test_assert_project_owned_by_rejects_wrong_learner(repo):
    _seed_user_project(repo, user_id="u1", project_id="p1")
    repo.add_user(User(user_id="u2"))
    with pytest.raises(ScopeError):
        repo.assert_project_owned_by("p1", "u2")


def test_assert_project_owned_by_unknown_project(repo):
    _seed_user_project(repo)
    with pytest.raises(ScopeError):
        repo.assert_project_owned_by("nope", "u1")


# --- books / scoping ------------------------------------------------------

def test_one_primary_book_per_project(repo):
    _seed_user_project(repo)
    _seed_book(repo, book_id="b1")
    repo.add_book(Book(book_id="b2", owner_user_id="u1", source_hash="b2", title="x"))
    with pytest.raises(ScopeError):
        repo.link_book(ProjectBook(project_id="p1", book_id="b2", role=BookRole.PRIMARY))


def test_link_book_unique_per_project(repo):
    _seed_user_project(repo)
    _seed_book(repo, book_id="b1")
    with pytest.raises(ScopeError):
        repo.link_book(ProjectBook(project_id="p1", book_id="b1", role=BookRole.PRIMARY))


def test_allowed_book_ids_respects_enabled(repo):
    _seed_user_project(repo)
    _seed_book(repo, book_id="b1")
    repo.add_book(Book(book_id="b2", owner_user_id="u1", source_hash="b2", title="ref"))
    repo.link_book(ProjectBook(project_id="p1", book_id="b2", role=BookRole.REFERENCE,
                               enabled_for_retrieval=False))
    assert repo.allowed_book_ids("p1") == {"b1"}
    assert repo.allowed_book_ids("p1", only_enabled=False) == {"b1", "b2"}


# --- concepts -------------------------------------------------------------

def test_concept_scope_isolation(repo):
    _seed_user_project(repo, user_id="u1", project_id="p1")
    _seed_book(repo, project_id="p1", book_id="b1")
    repo.add_concept(Concept(concept_id="c_ref", book_id="b1", name="Reference"))
    assert repo.concept_in_project_scope("c_ref", "p1") is True
    # second user/project with a different book — c_ref is not in scope
    _seed_user_project(repo, user_id="u2", project_id="p2")
    _seed_book(repo, project_id="p2", book_id="b2", learner_id="u2")
    assert repo.concept_in_project_scope("c_ref", "p2") is False


# --- learner state --------------------------------------------------------

def test_get_state_returns_default_when_absent(repo):
    _seed_user_project(repo)
    s = repo.get_state("p1", "c_ref")
    assert s.current_verified_level == Level.L0
    assert s.exposure_state.value == "NONE"


def test_save_state_roundtrip(repo):
    _seed_user_project(repo)
    s = repo.get_state("p1", "c_ref")
    s.current_verified_level = Level.L1
    s.bump_version()
    repo.save_state(s)
    again = repo.get_state("p1", "c_ref")
    assert again.current_verified_level == Level.L1
    assert again.version == 1


# --- evidence idempotency -------------------------------------------------

def _make_evidence(eid, ekey, project_id="p1", concept_id="c_ref", book_id="b1"):
    return Evidence(
        evidence_id=eid, event_key=ekey, project_id=project_id, concept_id=concept_id,
        source_book_id=book_id, evidence_type=EvidenceType.VERIFY,
        required_level=Level.L1, result=EvidenceResult.PASS, independent=True,
    )


def test_append_evidence_idempotent_on_event_key(repo):
    _seed_user_project(repo)
    _seed_book(repo)
    e1 = _make_evidence("e1", "key-1")
    e2 = _make_evidence("e2", "key-1")  # same event_key, different evidence_id
    assert repo.append_evidence(e1) is True
    assert repo.append_evidence(e2) is False  # idempotent
    assert len(repo.evidence_for("p1", "c_ref")) == 1


def test_append_evidence_different_key_writes_both(repo):
    """Idempotency is on event_key only (matches InMemoryRepository): a
    different key with a different evidence_id writes a second row."""
    _seed_user_project(repo)
    _seed_book(repo)
    assert repo.append_evidence(_make_evidence("e1", "key-1")) is True
    assert repo.append_evidence(_make_evidence("e2", "key-2")) is True
    assert len(repo.evidence_for("p1", "c_ref")) == 2


def test_evidence_for_misconception_matches_signals_and_changed_task(repo):
    _seed_user_project(repo)
    _seed_book(repo)
    sig = [MisconceptionSignal(bug_id="bug_x", direction="FOR", strength="STRONG")]
    e = Evidence(
        evidence_id="e1", event_key="k1", project_id="p1", concept_id="c_ref",
        source_book_id="b1", evidence_type=EvidenceType.VERIFY,
        required_level=Level.L1, result=EvidenceResult.FAIL,
        misconception_signals=sig,
    )
    repo.append_evidence(e)
    found = repo.evidence_for_misconception("p1", "bug_x")
    assert len(found) == 1
    # changed-task linked via discriminated_bug_ids
    e2 = Evidence(
        evidence_id="e2", event_key="k2", project_id="p1", concept_id="c_ref",
        source_book_id="b1", evidence_type=EvidenceType.CHANGED_TASK,
        required_level=Level.L2, result=EvidenceResult.PASS,
        discriminated_bug_ids=["bug_x"],
    )
    repo.append_evidence(e2)
    found = repo.evidence_for_misconception("p1", "bug_x")
    assert len(found) == 2


# --- misconceptions -------------------------------------------------------

def test_upsert_misconception_roundtrip(repo):
    _seed_user_project(repo)
    mis = MisconceptionHypothesis(project_id="p1", bug_id="bug_x", status="SUSPECTED",
                                   evidence_score=3)
    repo.upsert_misconception(mis)
    got = repo.get_misconception("p1", "bug_x")
    assert got is not None
    assert got.evidence_score == 3
    # update via model_copy so Pydantic re-coerces the enum (direct attribute
    # assignment would bypass validation).
    updated = got.model_copy(update={"evidence_score": 7, "status": "CONFIRMED"})
    repo.upsert_misconception(updated)
    again = repo.get_misconception("p1", "bug_x")
    assert again.evidence_score == 7
    assert again.status == "CONFIRMED"


def test_all_misconceptions_scoped_to_project(repo):
    _seed_user_project(repo, user_id="u1", project_id="p1")
    _seed_user_project(repo, user_id="u2", project_id="p2")
    repo.upsert_misconception(MisconceptionHypothesis(project_id="p1", bug_id="b1"))
    repo.upsert_misconception(MisconceptionHypothesis(project_id="p2", bug_id="b2"))
    assert {m.bug_id for m in repo.all_misconceptions("p1")} == {"b1"}
    assert {m.bug_id for m in repo.all_misconceptions("p2")} == {"b2"}


# --- transitions & expiry -------------------------------------------------

def test_record_transition_and_expiry_keys(repo):
    _seed_user_project(repo)
    repo.record_transition(StateTransition(
        transition_id="t1", entity_type="mastery", entity_id="c_ref",
        project_id="p1", old_state="L0", new_state="L1", rule_version="rule_v1",
    ))
    repo.record_expiry_key("exp-1")
    repo.record_expiry_key("exp-1")  # idempotent
    assert repo.expiry_keys() == {"exp-1"}


# --- cross-project isolation ---------------------------------------------

def test_cross_project_state_isolation(repo):
    _seed_user_project(repo, user_id="u1", project_id="p1")
    _seed_book(repo, project_id="p1", book_id="b1")
    repo.add_concept(Concept(concept_id="c_ref", book_id="b1", name="ref"))
    _seed_user_project(repo, user_id="u2", project_id="p2")
    _seed_book(repo, project_id="p2", book_id="b2", learner_id="u2")
    repo.add_concept(Concept(concept_id="c_ref", book_id="b2", name="ref"))

    s1 = repo.get_state("p1", "c_ref")
    s1.current_verified_level = Level.L1
    s1.bump_version()
    repo.save_state(s1)

    # p2's c_ref is unaffected.
    s2 = repo.get_state("p2", "c_ref")
    assert s2.current_verified_level == Level.L0
    assert s2.version == 0


# --- evidence_for_project (M4) -------------------------------------------

def test_evidence_for_project_scoped(repo):
    _seed_user_project(repo, user_id="u1", project_id="p1")
    _seed_book(repo, project_id="p1", book_id="b1")
    _seed_user_project(repo, user_id="u2", project_id="p2")
    _seed_book(repo, project_id="p2", book_id="b2", learner_id="u2")
    repo.append_evidence(_make_evidence("e1", "k1", project_id="p1", book_id="b1"))
    repo.append_evidence(_make_evidence("e2", "k2", project_id="p2", concept_id="c_other",
                                        book_id="b2"))
    p1 = repo.evidence_for_project("p1")
    assert len(p1) == 1
    assert p1[0].evidence_id == "e1"


# --- trusted tasks / submissions (M4) ------------------------------------

def _make_task_data(task_id="t1", project_id="p1", conversation_id="c1", learner_id="u1",
                    status="PENDING"):
    return {
        "task_id": task_id, "project_id": project_id, "conversation_id": conversation_id,
        "run_id": "r1", "learner_id": learner_id, "task_version": 1,
        "target_concept_ids": ["c_ref"], "evidence_for_levels": ["L2"],
        "rubric": ["explains aliasing"], "allowed_resources": [], "source_refs": [],
        "scenario_fingerprint": None, "is_probe": True,
        "discriminated_bug_ids": ["bug_ref_vs_object"], "is_changed_task": False,
        "remediation_stage": 0, "prompt_text": "Box a = new Box(1); ...",
        "expected_answer": "9", "distractors": [],
        "status": status, "hints_issued": 0, "last_submission_id": None,
        "created_at": None, "expires_at": None,
    }


def test_trusted_task_save_get_roundtrip(repo):
    _seed_user_project(repo)
    d = _make_task_data()
    repo.save_trusted_task(d)
    got = repo.get_trusted_task("t1")
    assert got is not None
    assert got["prompt_text"] == d["prompt_text"]
    assert got["is_probe"] is True
    assert got["discriminated_bug_ids"] == ["bug_ref_vs_object"]
    assert got["status"] == "PENDING"
    assert repo.get_trusted_task("missing") is None


def test_pending_task_lookup(repo):
    _seed_user_project(repo)
    repo.save_trusted_task(_make_task_data(task_id="t1", conversation_id="c1"))
    assert repo.pending_task_for_conversation("c1") is not None
    assert repo.pending_task_for_conversation("c1")["task_id"] == "t1"
    assert repo.pending_task_for_conversation("other") is None
    assert repo.pending_task_for_project("p1")["task_id"] == "t1"
    # Answered task is no longer pending.
    repo.update_task_status("t1", "ANSWERED", last_submission_id="s1")
    assert repo.pending_task_for_conversation("c1") is None
    assert repo.pending_task_for_project("p1") is None
    got = repo.get_trusted_task("t1")
    assert got["status"] == "ANSWERED"
    assert got["last_submission_id"] == "s1"


def test_increment_task_hints(repo):
    _seed_user_project(repo)
    repo.save_trusted_task(_make_task_data())
    assert repo.increment_task_hints("t1") == 1
    assert repo.increment_task_hints("t1") == 2
    assert repo.get_trusted_task("t1")["hints_issued"] == 2
    assert repo.increment_task_hints("missing") == 0


def test_submission_idempotent_on_task_idem_key(repo):
    _seed_user_project(repo)
    repo.save_trusted_task(_make_task_data())
    sub = {
        "submission_id": "s1", "task_id": "t1", "project_id": "p1", "learner_id": "u1",
        "answer_text": "9", "judgment": {"judgment_status": "DECIDED", "result": "PASS"},
        "run_id": "r1", "idempotency_key": "idem-1", "created_at": None,
    }
    assert repo.save_submission(sub) is True
    # Same (task_id, idempotency_key) → idempotent no-op.
    sub_again = dict(sub, submission_id="s2")
    assert repo.save_submission(sub_again) is False
    got = repo.get_submission_by_idem("t1", "idem-1")
    assert got is not None
    assert got["submission_id"] == "s1"  # the original is retained
    assert got["judgment"]["result"] == "PASS"
    # A different idempotency_key writes a second submission.
    assert repo.save_submission(dict(sub, submission_id="s3", idempotency_key="idem-2")) is True
    assert repo.get_submission_by_idem("t1", "idem-2") is not None


def test_trusted_task_cross_project_isolation(repo):
    _seed_user_project(repo, user_id="u1", project_id="p1")
    _seed_user_project(repo, user_id="u2", project_id="p2")
    repo.save_trusted_task(_make_task_data(task_id="t1", project_id="p1", conversation_id="c1"))
    repo.save_trusted_task(_make_task_data(task_id="t2", project_id="p2", conversation_id="c2"))
    assert repo.pending_task_for_conversation("c1")["task_id"] == "t1"
    assert repo.pending_task_for_conversation("c2")["task_id"] == "t2"
    assert repo.pending_task_for_project("p1")["task_id"] == "t1"
    assert repo.pending_task_for_project("p2")["task_id"] == "t2"
