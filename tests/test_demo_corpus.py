"""L1/L3 tests: offline demo corpus — OPEN_SOURCE_REFERENCES §8.

The demo must run with no network, no PDF and no live model. This seeds the
corpus into a repo + retriever and runs a real Q&A loop offline.
"""

from __future__ import annotations

from bookmind.agents.demo_corpus import DemoCorpus, demo_learner_states
from bookmind.domain.models import LearningProject, User
from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.services import BookQAService
from bookmind.storage.in_memory import InMemoryRepository
from bookmind.domain.enums import BookRole
from bookmind.domain.models import Book, ProjectBook


def test_demo_corpus_has_chunks_and_concepts():
    corp = DemoCorpus()
    assert len(corp.chunks) >= 10  # 11 non-heading blocks
    assert len(corp.concepts) == 30  # gold skeleton
    # Every chunk has a source_ref with a real page.
    for c in corp.chunks:
        assert c.source_ref.physical_page >= 41
        assert c.section_path  # section path preserved


def test_demo_corpus_seeds_into_repo():
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="demo"))
    corp = DemoCorpus()
    repo.add_book(Book(book_id=corp.book_id, owner_user_id="u1", source_hash="demo", title="Java Core"))
    repo.link_book(ProjectBook(project_id="p1", book_id=corp.book_id, role=BookRole.PRIMARY))
    corp.seed_concepts_into(repo)
    corp.seed_chunks_into(repo)
    assert len(repo.concepts_for_book(corp.book_id)) == 30
    assert len(repo.chunks_for_book(corp.book_id)) == len(corp.chunks)


def test_demo_offline_qa_loop():
    """Full Q&A loop with the offline router: seed → retrieve → answer."""
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="demo"))
    corp = DemoCorpus()
    repo.add_book(Book(book_id=corp.book_id, owner_user_id="u1", source_hash="demo", title="Java Core"))
    repo.link_book(ProjectBook(project_id="p1", book_id=corp.book_id, role=BookRole.PRIMARY))
    corp.seed_concepts_into(repo)
    corp.seed_chunks_into(repo)

    router = ModelRouter(RouterConfig(live=False))  # offline
    retriever = corp.build_retriever(router)
    repo.set_retriever("p1", retriever)

    svc = BookQAService(repo, router)
    ans = svc.ask(project_id="p1", learner_id="u1", question="== 和 equals 的区别")
    # Offline model → fallback answer, but grounded in a real demo chunk.
    assert ans.grounded is True
    assert ans.chunk_ids
    # The chunk must be from the demo corpus.
    assert ans.chunk_ids[0].startswith("demo-chk-")


def test_demo_learner_states_have_different_gaps():
    states = demo_learner_states("p1")
    by_id = {s.concept_id: s for s in states}
    # c_variable verified at L2; c_hashcode unverified; c_reference_equality expired.
    assert by_id["c_variable"].current_verified_level.value == "L2"
    assert by_id["c_hashcode"].current_verified_level.value == "L0"
    assert by_id["c_reference_equality"].levels["L1"].status.value == "EXPIRED"
