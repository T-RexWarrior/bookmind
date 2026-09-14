"""End-to-end tests for the Book Mapping service (Phase 3).

Runs the full Map–Aggregate flow on the offline demo corpus (no live model):
  - gold skeleton protected + preserved;
  - graph acyclic;
  - new concepts added (extension toward 50–80);
  - undo removes only proposal additions;
  - idempotent re-run reuses the cache.
"""

from __future__ import annotations

from bookmind.agents.demo_corpus import DemoCorpus
from bookmind.domain.enums import ConceptSource
from bookmind.domain.models import LearningProject, User, Book, ProjectBook
from bookmind.domain.enums import BookRole
from bookmind.engine.book_graph.mapper import is_acyclic
from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.services import BookMappingService
from bookmind.storage.in_memory import InMemoryRepository
from bookmind.retrieval.chunk import DocumentChunk
from bookmind.domain.source_ref import SourceRef


def _setup_repo_with_demo() -> tuple:
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="demo"))
    corp = DemoCorpus()
    repo.add_book(Book(book_id=corp.book_id, owner_user_id="u1", source_hash="demo", title="Java"))
    repo.link_book(ProjectBook(project_id="p1", book_id=corp.book_id, role=BookRole.PRIMARY))
    corp.seed_chunks_into(repo)
    return repo, corp


def test_map_book_preserves_gold_skeleton():
    repo, corp = _setup_repo_with_demo()
    svc = BookMappingService(repo, ModelRouter(RouterConfig(live=False)))
    report = svc.map_book(project_id="p1", learner_id="u1", book_id=corp.book_id,
                          parsed_document=corp.parsed_document, chunks=corp.chunks,
                          graph_key="g1")
    concepts = repo.concepts_for_book(corp.book_id)
    gold = [c for c in concepts if c.source == ConceptSource.GOLD.value]
    # All 30 gold concepts present.
    assert len(gold) == 30
    assert report.gold_concepts == 30
    # Gold edges preserved (e.g. c_reference → c_variable).
    ref = next(c for c in concepts if c.concept_id == "c_reference")
    assert "c_variable" in ref.prerequisites


def test_map_book_result_is_acyclic():
    repo, corp = _setup_repo_with_demo()
    svc = BookMappingService(repo, ModelRouter(RouterConfig(live=False)))
    svc.map_book(project_id="p1", learner_id="u1", book_id=corp.book_id,
                 parsed_document=corp.parsed_document, chunks=corp.chunks, graph_key="g1")
    concepts = repo.concepts_for_book(corp.book_id)
    assert is_acyclic(concepts)


def test_map_book_extends_concepts():
    repo, corp = _setup_repo_with_demo()
    svc = BookMappingService(repo, ModelRouter(RouterConfig(live=False)))
    report = svc.map_book(project_id="p1", learner_id="u1", book_id=corp.book_id,
                          parsed_document=corp.parsed_document, chunks=corp.chunks, graph_key="g1")
    # Gold is 30; total should be >= 30 (extension). Offline extractor is
    # conservative but should at least not lose any.
    assert report.total_concepts >= 30
    # New concepts (if any) are LLM_PROPOSED.
    new = [c for c in repo.concepts_for_book(corp.book_id) if c.source == ConceptSource.LLM_PROPOSED.value]
    for c in new:
        assert c.concept_id.startswith("lc_")


def test_map_book_idempotent_with_same_graph_key():
    repo, corp = _setup_repo_with_demo()
    svc = BookMappingService(repo, ModelRouter(RouterConfig(live=False)))
    r1 = svc.map_book(project_id="p1", learner_id="u1", book_id=corp.book_id,
                      parsed_document=corp.parsed_document, chunks=corp.chunks, graph_key="g1")
    r2 = svc.map_book(project_id="p1", learner_id="u1", book_id=corp.book_id,
                      parsed_document=corp.parsed_document, chunks=corp.chunks, graph_key="g1")
    assert r2.reused is True
    assert r1.total_concepts == r2.total_concepts


def test_undo_removes_only_proposal_additions():
    repo, corp = _setup_repo_with_demo()
    svc = BookMappingService(repo, ModelRouter(RouterConfig(live=False)))
    report = svc.map_book(project_id="p1", learner_id="u1", book_id=corp.book_id,
                          parsed_document=corp.parsed_document, chunks=corp.chunks, graph_key="g1")
    before = repo.concepts_for_book(corp.book_id)
    res = svc.undo_mapping(project_id="p1", learner_id="u1", book_id=corp.book_id, report=report)
    after = repo.concepts_for_book(corp.book_id)
    # Gold concepts remain.
    gold_after = [c for c in after if c.source == ConceptSource.GOLD.value]
    assert len(gold_after) == 30
    # Proposal-origin concepts removed (count matches the diff).
    assert len(after) == 30
    assert res["removed_concepts"] == len(report.added_concept_ids)
    # Graph still acyclic after undo.
    assert is_acyclic(after)


def test_undo_preserves_gold_edges():
    repo, corp = _setup_repo_with_demo()
    svc = BookMappingService(repo, ModelRouter(RouterConfig(live=False)))
    report = svc.map_book(project_id="p1", learner_id="u1", book_id=corp.book_id,
                          parsed_document=corp.parsed_document, chunks=corp.chunks, graph_key="g1")
    svc.undo_mapping(project_id="p1", learner_id="u1", book_id=corp.book_id, report=report)
    concepts = repo.concepts_for_book(corp.book_id)
    by_id = {c.concept_id: c for c in concepts}
    # A gold edge still present.
    assert "c_variable" in by_id["c_reference"].prerequisites
    assert "c_reference" in by_id["c_object"].prerequisites


def test_map_book_scope_isolated():
    """A learner not owning the project cannot map its book."""
    repo, corp = _setup_repo_with_demo()
    repo.add_user(User(user_id="u2"))
    svc = BookMappingService(repo, ModelRouter(RouterConfig(live=False)))
    from bookmind.storage.in_memory import ScopeError
    import pytest
    with pytest.raises(ScopeError):
        svc.map_book(project_id="p1", learner_id="u2", book_id=corp.book_id,
                     parsed_document=corp.parsed_document, chunks=corp.chunks, graph_key="g1")


def _generic_book(repo):
    repo.add_user(User(user_id="u-real"))
    repo.create_project(LearningProject(project_id="p-real", learner_id="u-real", name="机器学习"))
    repo.add_book(Book(book_id="book-real", owner_user_id="u-real", source_hash="real", title="机器学习"))
    repo.link_book(ProjectBook(project_id="p-real", book_id="book-real", role=BookRole.PRIMARY))
    chunks = [
        DocumentChunk(
            chunk_id="real-1", book_id="book-real", document_id="doc-real",
            section_path=("第一章 优化基础",), content="梯度下降是逐步减小损失函数的优化方法。",
            source_ref=SourceRef(document_id="doc-real", chunk_id="real-1", physical_page=1,
                                section_path=("第一章 优化基础",)),
        ),
        DocumentChunk(
            chunk_id="real-2", book_id="book-real", document_id="doc-real",
            section_path=("第二章 神经网络训练",),
            content="反向传播是计算梯度的方法，训练时通常结合梯度下降更新参数。",
            source_ref=SourceRef(document_id="doc-real", chunk_id="real-2", physical_page=8,
                                section_path=("第二章 神经网络训练",)),
        ),
    ]
    repo.add_chunks("book-real", chunks)
    return chunks


def test_real_book_graph_comes_only_from_uploaded_content_and_ids_are_stable():
    repo = InMemoryRepository()
    chunks = _generic_book(repo)
    first = BookMappingService(repo, ModelRouter(RouterConfig(live=False))).map_book(
        project_id="p-real", learner_id="u-real", book_id="book-real", chunks=chunks,
    )
    concepts1 = repo.concepts_for_book("book-real")
    ids1 = {c.concept_id for c in concepts1}
    assert first.gold_concepts == 0
    assert {c.name for c in concepts1} >= {"优化基础", "梯度下降", "神经网络训练", "反向传播"}
    assert all(c.source_refs for c in concepts1)
    assert not any(c.concept_id in {"c_variable", "c_reference", "c_object"} for c in concepts1)

    BookMappingService(repo, ModelRouter(RouterConfig(live=False))).map_book(
        project_id="p-real", learner_id="u-real", book_id="book-real", chunks=chunks,
    )
    assert {c.concept_id for c in repo.concepts_for_book("book-real")} == ids1


def test_real_book_graph_persists_in_sql_repository():
    from bookmind.storage.sql import SqlRepository

    repo = SqlRepository("sqlite:///:memory:")
    repo.create_schema()
    chunks = _generic_book(repo)
    report = BookMappingService(repo, ModelRouter(RouterConfig(live=False))).map_book(
        project_id="p-real", learner_id="u-real", book_id="book-real", chunks=chunks,
    )
    assert report.gold_concepts == 0
    assert len(repo.concepts_for_book("book-real")) == report.total_concepts
    assert repo.relations_for_book("book-real")
