"""Tests for the graph quality gold set (Phase 3).

The gold set is the acceptance bar for a built book graph. These tests verify
the checks themselves catch the right failures (mutation testing) and that the
offline demo build passes the full set.
"""

from __future__ import annotations

from bookmind.agents.demo_corpus import DemoCorpus
from bookmind.agents.concept_skeleton import PREREQUISITE_EDGES, skeleton_concept_ids
from bookmind.domain.enums import ConceptSource, RelationType
from bookmind.domain.models import Concept, ConceptRelation
from bookmind.evaluation.graph_gold_set import run_graph_gold_set, check_acyclic, check_gold_skeleton_preserved
from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.services import BookMappingService
from bookmind.domain.models import LearningProject, User, Book, ProjectBook
from bookmind.domain.enums import BookRole
from bookmind.storage.in_memory import InMemoryRepository


def _build_demo_graph() -> list[Concept]:
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="demo"))
    corp = DemoCorpus()
    repo.add_book(Book(book_id=corp.book_id, owner_user_id="u1", source_hash="demo", title="Java"))
    repo.link_book(ProjectBook(project_id="p1", book_id=corp.book_id, role=BookRole.PRIMARY))
    corp.seed_chunks_into(repo)
    svc = BookMappingService(repo, ModelRouter(RouterConfig(live=False)))
    svc.map_book(project_id="p1", learner_id="u1", book_id=corp.book_id,
                 parsed_document=corp.parsed_document, chunks=corp.chunks, graph_key="g1")
    return repo.concepts_for_book(corp.book_id)


def test_demo_build_passes_full_gold_set():
    concepts = _build_demo_graph()
    results = run_graph_gold_set(concepts, count_range=(50, 80))
    failed = [r for r in results if not r.ok]
    assert failed == [], f"failed checks: {[(r.name, r.detail) for r in failed]}"
    names = {r.name for r in results}
    assert "acyclic" in names and "gold_skeleton_preserved" in names


# --- mutation tests: each check catches its failure ----------------------


def test_check_acyclic_detects_cycle():
    concepts = [
        Concept(concept_id="a", book_id="b", name="A", prerequisites=["b"]),
        Concept(concept_id="b", book_id="b", name="B", prerequisites=["a"]),
    ]
    assert check_acyclic(concepts).ok is False


def test_check_gold_skeleton_preserved_detects_missing():
    concepts = [Concept(concept_id="c_variable", book_id="b", name="V",
                        source=ConceptSource.GOLD.value)]
    res = check_gold_skeleton_preserved(concepts)
    assert res.ok is False
    assert "c_reference" in res.detail


def test_check_gold_skeleton_detects_source_change():
    # c_variable present but source flipped to LLM_PROPOSED.
    concepts = [
        Concept(concept_id=cid, book_id="b", name=cid,
                source=ConceptSource.LLM_PROPOSED.value if cid == "c_variable" else ConceptSource.GOLD.value)
        for cid in skeleton_concept_ids()
    ]
    res = check_gold_skeleton_preserved(concepts)
    assert res.ok is False
    assert "c_variable" in res.detail


def test_gold_edges_intact_detects_removed_edge():
    concepts = _build_demo_graph()
    # Mutate: remove a gold edge from c_reference.
    for c in concepts:
        if c.concept_id == "c_reference":
            c.prerequisites = [p for p in c.prerequisites if p != "c_variable"]
    results = run_graph_gold_set(concepts, count_range=(50, 80))
    edge_check = next(r for r in results if r.name == "gold_edges_intact")
    assert edge_check.ok is False


def test_source_traceable_detects_missing_refs():
    concepts = _build_demo_graph()
    # Strip source_refs from an LLM concept.
    for c in concepts:
        if c.source == ConceptSource.LLM_PROPOSED.value:
            c.source_refs = []
            break
    results = run_graph_gold_set(concepts, count_range=(50, 80))
    trace = next(r for r in results if r.name == "source_traceable")
    assert trace.ok is False


def test_no_orphan_prerequisites_detects_dangling():
    concepts = _build_demo_graph()
    # Add a dangling prerequisite to a gold concept.
    for c in concepts:
        if c.concept_id == "c_variable":
            c.prerequisites.append("does_not_exist")
            break
    results = run_graph_gold_set(concepts, count_range=(50, 80))
    orphan = next(r for r in results if r.name == "no_orphan_prerequisites")
    assert orphan.ok is False
