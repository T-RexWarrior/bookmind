from __future__ import annotations

import sqlite3
import time

from bookmind.agents.tutor import TutorAgent
from bookmind.domain.source_ref import SourceRef
from bookmind.llm.router import ModelConfig, ModelRouter, RouterConfig
from bookmind.retrieval.bm25 import BM25Index
from bookmind.retrieval.chunk import DocumentChunk
from bookmind.retrieval.chunking import CHUNKER_VERSION, Chunker, estimate_tokens
from bookmind.retrieval.citation import CitationValidator
from bookmind.retrieval.fusion import HybridRetriever, RetrievalHit
from bookmind.retrieval.parsed_document import Block, Page, ParsedDocument, Section
from bookmind.retrieval.parsers.adaptive import AdaptivePdfParser
from bookmind.retrieval.parsers.base import DocumentParser, FileMetadata, ParseOptions
from bookmind.retrieval.parsers.quality import (
    looks_like_broken_cjk_font_map,
    quality_label,
    score_page_text,
)
from bookmind.retrieval.persistent_index import load_vectors, publish_index
from bookmind.retrieval.vector import VectorStore
from bookmind.api.routes.books import _apply_outline_to_chunks


def _page_doc(parser: str, scores: dict[int, float]) -> ParsedDocument:
    blocks = [
        Block(
            block_id=f"b{page}", text=f"第{page}页 数据结构与算法正文 " * 25,
            physical_page=page, reading_order=page, section_path=("第一章",),
            parser_name=parser, confidence=score,
        )
        for page, score in scores.items()
    ]
    pages = [
        Page(
            physical_page=page, block_ids=[f"b{page}"], parser_name=parser,
            quality_score=score, quality_label=quality_label(score),
        )
        for page, score in scores.items()
    ]
    return ParsedDocument(
        document_id="doc", source_file="book.pdf", source_hash="hash",
        parser=parser, parser_version=f"{parser}-1", pages=pages, blocks=blocks,
        sections=[Section(section_id="s1", title="第一章", section_path=("第一章",), physical_page=1)],
    )


class _FakeParser(DocumentParser):
    version = "fake-1"

    def __init__(self, name: str, doc: ParsedDocument):
        self.name = name
        self.doc = doc
        self.calls: list[tuple[int, ...]] = []

    def supports(self, meta):
        return 1.0

    def parse(self, source, meta, options=None):
        options = options or ParseOptions()
        self.calls.append(options.page_numbers)
        if not options.page_numbers:
            return self.doc
        selected = set(options.page_numbers)
        return self.doc.model_copy(update={
            "pages": [p for p in self.doc.pages if p.physical_page in selected],
            "blocks": [b for b in self.doc.blocks if b.physical_page in selected],
        })


def test_page_quality_detects_clean_text_and_garbage():
    clean, warnings = score_page_text("数据结构课程介绍数组链表栈队列树图算法复杂度分析。" * 20)
    broken, broken_warnings = score_page_text("�" * 300)
    assert clean >= 0.80 and not warnings
    assert broken < 0.45 and broken_warnings


def test_page_quality_detects_valid_unicode_from_broken_cjk_font_map():
    broken_text = (
        "返种方法记弽了元素癿次序，并在弼前范围内迕行刞断。" * 12
    )

    score, warnings = score_page_text(broken_text)

    assert looks_like_broken_cjk_font_map(broken_text)
    assert score < 0.80
    assert "中文字体映射疑似损坏" in warnings


def test_adaptive_parser_only_ocrs_suspicious_pages():
    native = _FakeParser("native", _page_doc("native", {1: 0.92, 2: 0.20}))
    ocr = _FakeParser("rapidocr", _page_doc("rapidocr", {1: 0.90, 2: 0.88}))
    parser = AdaptivePdfParser(native=native, ocr=ocr)
    result = parser.parse(b"%PDF-", FileMetadata("book.pdf"))
    assert ocr.calls == [(2,)]
    assert result.pages[0].parser_name == "native"
    assert result.pages[1].parser_name == "rapidocr"
    assert result.pipeline_version == "pipeline_v2"


def test_parent_child_chunks_keep_page_range_and_bound_oversized_text():
    long_text = "算法分析用于比较程序性能。" * 300
    doc = ParsedDocument(
        document_id="d1", source_file="book.pdf", source_hash="h",
        parser="test", parser_version="1",
        sections=[Section(section_id="s", title="第一章", section_path=("第一章",), physical_page=1)],
        blocks=[
            Block(block_id="b1", block_type="heading", text="第一章", physical_page=1, reading_order=0, section_path=("第一章",)),
            Block(block_id="b2", text=long_text, physical_page=1, reading_order=1, section_path=("第一章",)),
            Block(block_id="b3", text="跨页结论", physical_page=2, reading_order=2, section_path=("第一章",)),
        ],
    )
    chunks = Chunker(target_tokens=80, overlap_tokens=10).chunk(doc, "book")
    assert len(chunks) > 2
    assert all(chunk.parent_chunk_id for chunk in chunks)
    assert all(chunk.chunker_version == CHUNKER_VERSION for chunk in chunks)
    assert max(estimate_tokens(chunk.content) for chunk in chunks) <= 170
    assert any(chunk.page_end == 2 for chunk in chunks)


def test_hybrid_retrieval_runs_both_channels_and_marks_evidence():
    chunk = DocumentChunk(
        chunk_id="c1", book_id="b1", document_id="d1",
        section_path=("第五章",), content="二叉搜索树的删除操作需要处理三种节点情况。",
        source_ref=SourceRef(document_id="d1", chunk_id="c1", physical_page=182),
        page_start=182, page_end=185,
    )
    retriever = HybridRetriever(BM25Index(), VectorStore(), ModelRouter(RouterConfig(live=False)), rerank_enabled=False)
    retriever.index_chunks([chunk])
    hit = retriever.retrieve("二叉搜索树删除操作", context_budget=1)[0]
    assert hit.bm25_rank == 1
    assert hit.dense_rank == 1
    assert hit.confidence_label == "HIGH"


def test_atomic_persistent_index_and_location_only_model_fallback(tmp_path):
    chunk = DocumentChunk(
        chunk_id="c1", book_id="b1", document_id="d1",
        section_path=("第一章",), content="变量是一个存储位置的名称。",
        source_ref=SourceRef(document_id="d1", chunk_id="c1", physical_page=10),
        page_start=10, page_end=10,
    )
    publish_index(tmp_path, [chunk], vectors=[[0.1, 0.2]], embedding_model="embedding-v1")
    ids, matrix, model = load_vectors(tmp_path)
    assert ids == ["c1"] and matrix is not None and model == "embedding-v1"
    with sqlite3.connect(tmp_path / "keywords.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 1

    validator = CitationValidator({"c1": chunk}, {"b1"})
    tutor = TutorAgent(ModelRouter(RouterConfig(live=False)), validator)
    answer = tutor.answer("什么是变量", [RetrievalHit(chunk=chunk)])
    assert answer.fallback is True
    assert answer.text == ""
    assert "变量是一个" not in answer.text


def test_manual_outline_reassigns_chunk_location_without_changing_text():
    chunk = DocumentChunk(
        chunk_id="c1", book_id="b1", document_id="d1",
        section_path=("旧目录",), content="正文保持不变",
        source_ref=SourceRef(
            document_id="d1", chunk_id="c1", physical_page=8,
            section_path=("旧目录",),
        ),
        page_start=8, page_end=9,
    )
    revised = _apply_outline_to_chunks([chunk], [
        {"title": "第一章", "page": 1, "path": ["第一章"]},
        {"title": "1.2 新目录", "page": 7, "path": ["第一章", "1.2 新目录"]},
    ])
    assert revised[0].content == chunk.content
    assert revised[0].chunk_id == chunk.chunk_id
    assert revised[0].section_path == ("第一章", "1.2 新目录")
    assert revised[0].source_ref.section_path == revised[0].section_path


def test_product_message_api_returns_async_run_and_finishes(tmp_path):
    from fastapi.testclient import TestClient
    from bookmind.api.app import create_app
    from bookmind.storage.sql import SqlRepository

    repo = SqlRepository(f"sqlite:///{tmp_path}/bookmind.db")
    repo.create_schema()
    with TestClient(create_app(repo=repo)) as client:
        user_id = client.post("/api/session/bootstrap").json()["user_id"]
        project_id = client.post("/api/projects", json={"name": "测试空间"}).json()["project_id"]
        from bookmind.domain.enums import BookRole
        from bookmind.domain.models import Book, ProjectBook
        repo.add_book(Book(
            book_id="book-outline", owner_user_id=user_id,
            source_hash="outline-hash", title="目录测试",
        ))
        repo.link_book(ProjectBook(
            project_id=project_id, book_id="book-outline", role=BookRole.PRIMARY,
        ))
        outline_response = client.patch("/api/sources/book-outline/outline", json={
            "items": [
                {"title": "第一章", "page": 1, "path": ["第一章"]},
                {"title": "1.1 基础", "page": 3, "path": ["第一章", "1.1 基础"]},
            ],
        })
        assert outline_response.status_code == 200
        assert client.get("/api/sources/book-outline/outline").json()["items"][1]["page"] == 3
        conversation_id = client.post(
            f"/api/projects/{project_id}/conversations",
        ).json()["conversation_id"]
        started = time.perf_counter()
        response = client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "教材里如何定义这个概念？", "idempotency_key": "async-1"},
        )
        assert response.status_code == 202
        assert time.perf_counter() - started < 0.5
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = repo.get_run_record(run_id)
            if run and run.status in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.02)
        assert repo.get_run_record(run_id).status == "COMPLETED"
        assert any(message.role == "assistant" for message in repo.messages_for_conversation(conversation_id))


def test_model_router_opens_circuit_after_three_consecutive_failures(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret")
    calls: list[str] = []

    def failing_http(url, payload, key, timeout):
        calls.append(payload["model"])
        raise TimeoutError("timed out")

    router = ModelRouter(RouterConfig(
        chat_primary=ModelConfig("primary", "chat", retries=0),
        chat_fallbacks=(ModelConfig("backup", "chat", retries=0),),
        api_key_env="TEST_LLM_KEY", live=True,
        breaker_failures=3, breaker_cooldown=120,
    ), http=failing_http)
    for _ in range(3):
        assert not router.complete("test", [{"role": "user", "content": "hello"}]).ok
    before = len(calls)
    result = router.complete("test", [{"role": "user", "content": "hello"}])
    assert not result.ok
    assert len(calls) == before
