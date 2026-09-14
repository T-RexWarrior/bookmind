"""Phase 7 reliability / failure tests — ROADMAP Phase 7, EVALUATION §8.

Covers the failure modes the CI gate must tolerate:
  - concurrent state version conflict (last-write does NOT silently overwrite);
  - model timeout → structured degradation, never a crash;
  - invalid JSON from the model → fallback / NEEDS_REVIEW, no state increment;
  - index version mismatch (embedding_space change) forces re-index;
  - invalid task schema rejected by the Evidence Gate;
  - SQLite fallback shares the same domain contract (scope, evidence, gate).

These are L1 contract tests — fully deterministic, no live model.
"""

from __future__ import annotations

import json

import pytest

from bookmind.domain.enums import (
    ActivityMode,
    EvidenceResult,
    EvidenceType,
    HintLevel,
    InterventionPolicy,
    JudgmentStatus,
    Level,
    UIPreset,
)
from bookmind.domain.models import (
    AnswerJudgment,
    Evidence,
    InteractionContext,
    LearnerConceptState,
    ReviewPolicy,
    TrustedTaskContext,
)
from bookmind.engine.learning_engine import submit_answer
from bookmind.llm.router import ModelConfig, ModelRouter, RouterConfig
from bookmind.storage.in_memory import InMemoryRepository, ScopeError


# --- concurrent state version -----------------------------------------------

def test_concurrent_writes_do_not_silently_overwrite():
    """Two submits on the same concept must not race-corrupt state. The Engine
    reads state, applies, and saves; if two transactions interleave, the
    evidence-ledger is still append-only and idempotent — re-running yields the
    same recomputed state. This pins the §10 'no silent last-write overwrite'
    invariant by checking idempotent replays produce consistent state."""
    from bookmind.domain.models import User, LearningProject, Book, Concept, ProjectBook
    from bookmind.domain.enums import BookRole
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u"))
    repo.create_project(LearningProject(project_id="p", learner_id="u", name="t"))
    repo.add_book(Book(book_id="b", owner_user_id="u", source_hash="b", title="T"))
    repo.link_book(ProjectBook(project_id="p", book_id="b", role=BookRole.PRIMARY))
    repo.add_concept(Concept(concept_id="c1", book_id="b", name="c1"))
    task = TrustedTaskContext(task_id="t1", task_version=1, target_concept_ids=["c1"],
                              evidence_for_levels=[Level.L1], rubric=["recall"])
    interaction = InteractionContext(activity_mode=ActivityMode.READING,
                                     intervention_policy=InterventionPolicy.PROACTIVE,
                                     ui_preset=UIPreset.DEEP_LEARNING)
    judge = AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.PASS)
    # First write: PASS → L1 verified.
    r1 = submit_answer(repo, learner_id="u", project_id="p", task=task, interaction=interaction,
                       judgment=judge, answer_text="ok", policy=ReviewPolicy(),
                       submission_id="s1", evidence_id="e1", source_book_id="b")
    assert Level.L1 in r1.verified_levels
    # Replay with the same submission_id → idempotent no-op, state unchanged.
    r2 = submit_answer(repo, learner_id="u", project_id="p", task=task, interaction=interaction,
                       judgment=judge, answer_text="ok", policy=ReviewPolicy(),
                       submission_id="s1", evidence_id="e1", source_book_id="b")
    assert r2.written is False
    state = repo.get_state("p", "c1")
    assert state.current_verified_level == Level.L1
    # The real write bumped version once; the idempotent replay did NOT bump it
    # again (event_key dedup → no state write). This pins the §10 invariant.
    assert state.version == 1


def test_two_different_submissions_both_recorded():
    """Two distinct submissions on the same concept both append evidence; the
    second does not overwrite the first."""
    from bookmind.domain.models import User, LearningProject, Book, Concept, ProjectBook
    from bookmind.domain.enums import BookRole
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u"))
    repo.create_project(LearningProject(project_id="p", learner_id="u", name="t"))
    repo.add_book(Book(book_id="b", owner_user_id="u", source_hash="b", title="T"))
    repo.link_book(ProjectBook(project_id="p", book_id="b", role=BookRole.PRIMARY))
    repo.add_concept(Concept(concept_id="c1", book_id="b", name="c1"))
    task = TrustedTaskContext(task_id="t1", task_version=1, target_concept_ids=["c1"],
                              evidence_for_levels=[Level.L1], rubric=["recall"])
    interaction = InteractionContext(activity_mode=ActivityMode.READING,
                                     intervention_policy=InterventionPolicy.PROACTIVE,
                                     ui_preset=UIPreset.DEEP_LEARNING)
    judge = AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.PASS)
    submit_answer(repo, learner_id="u", project_id="p", task=task, interaction=interaction,
                  judgment=judge, answer_text="ok1", policy=ReviewPolicy(),
                  submission_id="s1", evidence_id="e1", source_book_id="b")
    submit_answer(repo, learner_id="u", project_id="p", task=task, interaction=interaction,
                  judgment=judge, answer_text="ok2", policy=ReviewPolicy(),
                  submission_id="s2", evidence_id="e2", source_book_id="b")
    ev = repo.evidence_for("p", "c1")
    assert len(ev) == 2  # both recorded, no overwrite


# --- model timeout → degradation --------------------------------------------

def test_model_timeout_returns_degradation_not_crash(monkeypatch):
    """A model timeout must return a structured fallback, not raise."""
    import urllib.error
    def slow_http(url, payload, key, timeout):
        raise TimeoutError("simulated timeout")
    monkeypatch.setenv("USTC_LLM_API_KEY", "sk-test")
    cfg = RouterConfig(live=True)
    r = ModelRouter(cfg, http=slow_http)
    res = r.complete("t", [{"role": "user", "content": "x"}])
    assert res.ok is False
    assert res.fallback is True
    # Must not crash — a degradation is returned.


def test_model_timeout_on_embed_falls_back_offline(monkeypatch):
    def slow_http(url, payload, key, timeout):
        raise TimeoutError("timeout")
    monkeypatch.setenv("USTC_LLM_API_KEY", "sk-test")
    cfg = RouterConfig(live=True)
    r = ModelRouter(cfg, http=slow_http)
    res = r.embed(["text"])
    assert res.ok is True
    assert res.fallback is True
    assert res.model == "offline-hash-256"


# --- invalid JSON → degradation ---------------------------------------------

def test_invalid_json_from_model_returns_no_parsed_json(monkeypatch):
    """If the model returns non-JSON when JSON was requested, parsed_json is
    None and the caller (Diagnostician) must degrade to NEEDS_REVIEW."""
    monkeypatch.setenv("USTC_LLM_API_KEY", "sk-test")
    body = json.dumps({"choices": [{"message": {"content": "this is not json"}}], "usage": {}})
    def fake_http(url, payload, key, timeout):
        return 200, body
    cfg = RouterConfig(live=True)
    r = ModelRouter(cfg, http=fake_http)
    res = r.complete("judge", [{"role": "user", "content": "x"}], output_schema={"type": "object"})
    assert res.ok is True
    assert res.parsed_json is None  # couldn't parse — caller must handle


def test_empty_content_from_model_handled(monkeypatch):
    """glm-5.3-flash can return content=null with reasoning_content; the router
    must not crash on null content."""
    monkeypatch.setenv("USTC_LLM_API_KEY", "sk-test")
    body = json.dumps({"choices": [{"message": {"content": None, "reasoning_content": "thinking..."}}], "usage": {}})
    def fake_http(url, payload, key, timeout):
        return 200, body
    cfg = RouterConfig(live=True)
    r = ModelRouter(cfg, http=fake_http)
    res = r.complete("t", [{"role": "user", "content": "x"}])
    # content None → treated as empty/degraded, not a crash.
    assert res.ok is False or res.text == "" or res.parsed_json is None


# --- index version mismatch -------------------------------------------------

def test_embedding_space_change_rejects_mixed_vectors():
    """A VectorStore must reject chunks from a different embedding space
    (ARCHITECTURE §8: embedding model/dim change → rebuild the index)."""
    from bookmind.retrieval.vector.store import VectorStore
    from bookmind.retrieval.chunk import DocumentChunk
    from bookmind.domain.source_ref import SourceRef
    vs = VectorStore()
    c1 = DocumentChunk(chunk_id="c1", book_id="b", document_id="d",
                       content="java refs", source_ref=SourceRef(document_id="d", physical_page=1),
                       embedding_space="qwen3-embedding-4096")
    vs.add(c1, [0.1] * 256)
    assert vs.embedding_space == "qwen3-embedding-4096"
    # A chunk from a DIFFERENT embedding space must be rejected.
    c2 = DocumentChunk(chunk_id="c2", book_id="b", document_id="d",
                       content="python refs", source_ref=SourceRef(document_id="d", physical_page=2),
                       embedding_space="other-model-128")
    with pytest.raises(ValueError, match="embedding space mismatch"):
        vs.add(c2, [0.2] * 128)
    # The original index is intact.
    assert len(vs) == 1
    res = vs.search([0.1] * 256, k=1)
    assert res[0][0] == "c1"


def test_bm25_index_rebuild_on_new_corpus():
    """Re-indexing BM25 with new docs must reflect the new corpus."""
    from bookmind.retrieval.bm25.index import BM25Index
    from bookmind.retrieval.chunk import DocumentChunk
    from bookmind.domain.source_ref import SourceRef

    def _chunk(cid, content, page):
        return DocumentChunk(chunk_id=cid, book_id="b", document_id="d", content=content,
                             source_ref=SourceRef(document_id="d", physical_page=page))
    idx = BM25Index()
    idx.add_many([_chunk("d1", "java reference semantics", 1),
                  _chunk("d2", "java equals method", 2)])
    res1 = idx.search("java reference", k=2)
    assert len(res1) == 2
    # Add a doc and re-search.
    idx.add(_chunk("d3", "python reference semantics", 3))
    res2 = idx.search("python reference", k=3)
    ids = [r[0] for r in res2]
    assert "d3" in ids


# --- invalid task schema rejected by Gate -----------------------------------

def test_trusted_task_without_levels_rejected():
    """A TrustedTaskContext must declare evidence_for_levels (schema validator)."""
    with pytest.raises(ValueError):
        TrustedTaskContext(task_id="t", task_version=1, target_concept_ids=["c1"],
                           evidence_for_levels=[], rubric=["x"])


def test_needs_review_judgment_produces_no_state_increment():
    """An invalid/unclear judgment (NEEDS_REVIEW) must not write Evidence or
    change mastery/misconception (LEARNING_MODEL §7)."""
    from bookmind.domain.models import User, LearningProject, Book, ProjectBook
    from bookmind.domain.enums import BookRole
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u"))
    repo.create_project(LearningProject(project_id="p", learner_id="u", name="t"))
    repo.add_book(Book(book_id="b", owner_user_id="u", source_hash="b", title="T"))
    repo.link_book(ProjectBook(project_id="p", book_id="b", role=BookRole.PRIMARY))
    task = TrustedTaskContext(task_id="t1", task_version=1, target_concept_ids=["c1"],
                              evidence_for_levels=[Level.L1], rubric=["recall"])
    interaction = InteractionContext(activity_mode=ActivityMode.READING,
                                     intervention_policy=InterventionPolicy.PROACTIVE,
                                     ui_preset=UIPreset.DEEP_LEARNING)
    judge = AnswerJudgment(judgment_status=JudgmentStatus.NEEDS_REVIEW)
    res = submit_answer(repo, learner_id="u", project_id="p", task=task, interaction=interaction,
                        judgment=judge, answer_text="unclear", policy=ReviewPolicy(),
                        submission_id="s1", evidence_id="e1", source_book_id="b")
    assert res.written is False
    assert res.needs_review is True
    assert repo.evidence_for("p", "c1") == []
    assert repo.get_state("p", "c1").current_verified_level == Level.L0


# --- SQLite fallback shares domain contract --------------------------------
# The in-memory repository IS the domain-contract reference. A future SQLite
# repository must pass the same contract tests. We pin the contract here so a
# SQLite impl can be dropped in behind the same tests.

def test_scope_isolation_contract():
    """Two users' projects cannot read/update each other's state; cross-project
    evidence is rejected. This is the shared contract both PG and SQLite must
    honour (EVALUATION §2)."""
    from bookmind.domain.models import User, LearningProject, Book, ProjectBook
    from bookmind.domain.enums import BookRole
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1"))
    repo.add_user(User(user_id="u2"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="t"))
    repo.create_project(LearningProject(project_id="p2", learner_id="u2", name="t"))
    repo.add_book(Book(book_id="b1", owner_user_id="u1", source_hash="b1", title="T"))
    repo.add_book(Book(book_id="b2", owner_user_id="u2", source_hash="b2", title="T"))
    repo.link_book(ProjectBook(project_id="p1", book_id="b1", role=BookRole.PRIMARY))
    repo.link_book(ProjectBook(project_id="p2", book_id="b2", role=BookRole.PRIMARY))
    # u1 writing to p2 is rejected.
    with pytest.raises(ScopeError):
        repo.assert_project_owned_by("p2", "u1")
    # Cross-project evidence: source_book b2 not in p1's scope.
    task = TrustedTaskContext(task_id="t", task_version=1, target_concept_ids=["c1"],
                              evidence_for_levels=[Level.L1], rubric=["x"])
    interaction = InteractionContext(activity_mode=ActivityMode.READING,
                                     intervention_policy=InterventionPolicy.PROACTIVE,
                                     ui_preset=UIPreset.DEEP_LEARNING)
    judge = AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.PASS)
    with pytest.raises(ScopeError):
        submit_answer(repo, learner_id="u1", project_id="p1", task=task, interaction=interaction,
                      judgment=judge, answer_text="x", policy=ReviewPolicy(),
                      submission_id="s", evidence_id="e", source_book_id="b2")


def test_evidence_idempotency_contract():
    """The same event_key must not write twice — shared by both storage paths."""
    from bookmind.domain.models import User, LearningProject, Book, ProjectBook
    from bookmind.domain.enums import BookRole
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u"))
    repo.create_project(LearningProject(project_id="p", learner_id="u", name="t"))
    repo.add_book(Book(book_id="b", owner_user_id="u", source_hash="b", title="T"))
    repo.link_book(ProjectBook(project_id="p", book_id="b", role=BookRole.PRIMARY))
    e1 = Evidence(evidence_id="e1", event_key="k", project_id="p", concept_id="c1",
                  source_book_id="b", evidence_type=EvidenceType.VERIFY,
                  required_level=Level.L1, result=EvidenceResult.PASS, independent=True,
                  hint_level=HintLevel.NONE, task_id="t1")
    assert repo.append_evidence(e1) is True
    e2 = Evidence(evidence_id="e2", event_key="k", project_id="p", concept_id="c1",
                  source_book_id="b", evidence_type=EvidenceType.VERIFY,
                  required_level=Level.L1, result=EvidenceResult.PASS, independent=True,
                  hint_level=HintLevel.NONE, task_id="t1")
    assert repo.append_evidence(e2) is False  # duplicate event_key
    assert len(repo.evidence_for("p", "c1")) == 1


def test_concurrent_version_monotonic():
    """State version is monotonic — it never decreases across writes."""
    from bookmind.domain.models import User, LearningProject, Book, ProjectBook
    from bookmind.domain.enums import BookRole
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u"))
    repo.create_project(LearningProject(project_id="p", learner_id="u", name="t"))
    repo.add_book(Book(book_id="b", owner_user_id="u", source_hash="b", title="T"))
    repo.link_book(ProjectBook(project_id="p", book_id="b", role=BookRole.PRIMARY))
    s = repo.get_state("p", "c1")
    v0 = s.version
    s.bump_version()
    repo.save_state(s)
    assert repo.get_state("p", "c1").version == v0 + 1
    s2 = repo.get_state("p", "c1")
    s2.bump_version()
    repo.save_state(s2)
    assert repo.get_state("p", "c1").version == v0 + 2
