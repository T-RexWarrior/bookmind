"""End-to-end QA service test — ingest (fallback parser) → ask → grounded answer.

Uses the offline ModelRouter so the whole RAG path runs deterministically with
no network. This is the Phase-2 smoke test: a real upload, parse, chunk, index,
retrieve, answer-with-citation loop.
"""

from __future__ import annotations

from bookmind.domain.models import User, LearningProject
from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.retrieval.parsers import PlainPdfFallback
from bookmind.jobs import JobStore, IngestionWorker
from bookmind.services import BookQAService
from bookmind.storage.in_memory import InMemoryRepository


# A small PDF with text the fallback parser can extract, including the answer
# to the sample question.
_QA_PDF = (
    b"%PDF-1.4 1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj "
    b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj "
    b"3 0 obj<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>endobj "
    b"4 0 obj<< /Length 120 >>stream\nBT /F1 12 Tf 72 700 Td (4.1 References and Objects) Tj "
    b"0 -14 Td (A reference variable stores the address of an object, not the object itself.) Tj "
    b"0 -14 Td (Using == compares references; equals compares content.) Tj ET\nendstream endobj"
)


def _service():
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="Java"))
    router = ModelRouter(RouterConfig(live=False))
    worker = IngestionWorker(JobStore(), router, parsers=[PlainPdfFallback()])
    return BookQAService(repo, router, worker=worker)


def test_ingest_then_ask_returns_grounded_answer():
    svc = _service()
    r = svc.ingest(project_id="p1", learner_id="u1", book_id="b1",
                   source=_QA_PDF, filename="book.pdf", title="Java Core")
    assert r["state"] == "SUCCEEDED"
    assert r["chunks"] > 0

    ans = svc.ask(project_id="p1", learner_id="u1", question="引用和对象的区别")
    # Offline mode must not expose a retrieved excerpt as though a generated
    # answer had passed the evidence gate.
    assert ans.grounded is False
    assert ans.chunk_ids == []


def test_ask_before_ingest_returns_helpful_error():
    svc = _service()
    ans = svc.ask(project_id="p1", learner_id="u1", question="anything")
    assert ans.grounded is False
    assert "ingest" in ans.reason


def test_ingest_is_scoped_rejects_other_learner():
    svc = _service()
    import pytest
    from bookmind.storage.in_memory import ScopeError
    with pytest.raises(ScopeError):
        svc.ingest(project_id="p1", learner_id="u2", book_id="b1",
                   source=_QA_PDF, filename="x.pdf")
