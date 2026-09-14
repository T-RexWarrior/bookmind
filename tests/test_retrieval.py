"""L1 tests: chunking, BM25, vector store, RRF fusion, citation.

All deterministic under the offline ModelRouter (no network). These exercise
the Hybrid RAG contract end-to-end (ARCHITECTURE §5, §4.2).
"""

from __future__ import annotations

from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.retrieval.bm25 import BM25Index
from bookmind.retrieval.chunk import DocumentChunk
from bookmind.retrieval.chunking import CHUNKER_VERSION, Chunker
from bookmind.retrieval.citation import CitationValidator
from bookmind.retrieval.fusion import HybridRetriever, reciprocal_rank_fusion
from bookmind.retrieval.parsed_document import Block, ParsedDocument, Section
from bookmind.retrieval.parsers import FileMetadata, ParseOptions, PlainPdfFallback
from bookmind.retrieval.vector import VectorStore
from bookmind.domain.source_ref import SourceRef


# --- fixtures ------------------------------------------------------------

def _doc_with_two_sections() -> ParsedDocument:
    blocks = [
        Block(block_id="b1", block_type="heading", text="3 变量与类型", physical_page=1, reading_order=0, section_path=("3 变量与类型",)),
        Block(block_id="b2", block_type="text", text="变量是一个存储位置的名称。每个变量都有类型。", physical_page=1, reading_order=1, section_path=("3 变量与类型",)),
        Block(block_id="b3", block_type="text", text="基本类型存储在栈上，引用类型指向堆上的对象。", physical_page=2, reading_order=2, section_path=("3 变量与类型",)),
        Block(block_id="b4", block_type="heading", text="4 引用与对象", physical_page=3, reading_order=3, section_path=("4 引用与对象",)),
        Block(block_id="b5", block_type="text", text="引用变量保存的是对象的地址，而不是对象本身。", physical_page=3, reading_order=4, section_path=("4 引用与对象",)),
        Block(block_id="b6", block_type="text", text="== 比较引用是否指向同一个对象，equals 比较内容。", physical_page=4, reading_order=5, section_path=("4 引用与对象",)),
    ]
    sections = [
        Section(section_id="s1", title="3 变量与类型", section_path=("3 变量与类型",), physical_page=1, block_ids=["b1", "b2", "b3"]),
        Section(section_id="s2", title="4 引用与对象", section_path=("4 引用与对象",), physical_page=3, block_ids=["b4", "b5", "b6"]),
    ]
    return ParsedDocument(
        document_id="d1", source_file="book.pdf", source_hash="h",
        parser="plain_pdf", parser_version="v1", blocks=blocks, sections=sections,
    )


def _chunk(doc, cid, content, page, section_path, block_ids, char_range=(0, 0)):
    return DocumentChunk(
        chunk_id=cid, book_id="book1", document_id=doc.document_id,
        section_path=section_path, content=content,
        source_ref=SourceRef(document_id=doc.document_id, chunk_id=cid, block_id=block_ids[0],
                             physical_page=page, section_path=section_path, char_range=char_range),
        block_ids=block_ids, char_range=char_range,
    )


# --- chunking ------------------------------------------------------------

def test_chunker_respects_section_boundaries():
    doc = _doc_with_two_sections()
    chunker = Chunker(target_tokens=8, overlap_tokens=2)
    chunks = chunker.chunk(doc, "book1")
    assert len(chunks) >= 2
    # No chunk spans both sections.
    for c in chunks:
        assert len(c.section_path) == 1
        assert c.section_path[0] in ("3 变量与类型", "4 引用与对象")
    # Every chunk has a source_ref with a real page and block.
    for c in chunks:
        assert c.source_ref.physical_page >= 1
        assert c.block_ids


def test_chunker_versioned_and_deterministic():
    doc = _doc_with_two_sections()
    chunker = Chunker(target_tokens=8, overlap_tokens=2)
    a = chunker.chunk(doc, "book1")
    b = chunker.chunk(doc, "book1")
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    assert all(c.chunker_version == CHUNKER_VERSION for c in a)


def test_chunk_ids_are_unique_across_sections_and_content_stays_readable():
    chunks = Chunker(target_tokens=8, overlap_tokens=2).chunk(
        _doc_with_two_sections(), "book1",
    )
    ids = [chunk.chunk_id for chunk in chunks]
    assert len(ids) == len(set(ids))
    assert all(chunk.source_ref.chunk_id == chunk.chunk_id for chunk in chunks)
    assert any("引用变量保存的是对象的地址" in chunk.content for chunk in chunks)
    assert all("引 用 变 量" not in chunk.content for chunk in chunks)


# --- BM25 ----------------------------------------------------------------

def test_bm25_ranks_relevant_chunk_first():
    doc = _doc_with_two_sections()
    chunks = [
        _chunk(doc, "ck1", "变量是一个存储位置的名称。每个变量都有类型。", 1, ("3 变量与类型",), ["b2"]),
        _chunk(doc, "ck2", "引用变量保存的是对象的地址，而不是对象本身。", 3, ("4 引用与对象",), ["b5"]),
        _chunk(doc, "ck3", "== 比较引用是否指向同一个对象，equals 比较内容。", 4, ("4 引用与对象",), ["b6"]),
    ]
    idx = BM25Index()
    idx.add_many(chunks)
    res = idx.search("引用与对象", k=3)
    assert res
    top = res[0][0]
    # The reference/object chunk should outrank the variables chunk for this query.
    assert top in ("ck2", "ck3")


def test_bm25_bitmap_question_prefers_the_unsupported_operation():
    doc = _doc_with_two_sections()
    chunks = [
        _chunk(doc, "ck1", "改进版 bitmap 的以上方法仅限于标记操作 set，尚不支持清除操作 clear。", 486, ("bitmap",), ["b1"]),
        _chunk(doc, "ck2", "进一步改进后同时支持 set 和 clear 两种操作。", 487, ("bitmap",), ["b2"]),
        _chunk(doc, "ck3", "动态数组容量不足时可以扩容。", 436, ("数组",), ["b3"]),
    ]
    idx = BM25Index()
    idx.add_many(chunks)
    assert idx.search("改进版 bitmap 目前不支持什么功能", k=3)[0][0] == "ck1"


def test_bm25_empty_and_idempotent():
    idx = BM25Index()
    assert idx.search("anything", k=3) == []
    c = _chunk(_doc_with_two_sections(), "ck1", "hello world", 1, ("s",), ["b1"])
    idx.add(c)
    idx.add(c)  # idempotent
    assert len(idx) == 1


# --- vector store --------------------------------------------------------

def test_vector_store_cosine_and_space_guard():
    vs = VectorStore()
    base = _chunk(_doc_with_two_sections(), "ck1", "a", 1, ("s",), ["b1"])
    c1 = base.model_copy(update={"embedding_space": "qwen3-embedding"})
    c2 = c1.model_copy(update={"chunk_id": "ck2"})
    vs.add(c1, [1.0, 0.0])
    vs.add(c2, [0.9, 0.1])
    res = vs.search([1.0, 0.0], k=2)
    assert res[0][0] == "ck1"  # exact match ranks first
    # Mismatched embedding space must be rejected.
    import pytest
    c3 = c1.model_copy(update={"chunk_id": "ck3", "embedding_space": "other"})
    with pytest.raises(ValueError):
        vs.add(c3, [1.0])


# --- RRF -----------------------------------------------------------------

def test_rrf_fuses_and_promotes_both_sources():
    bm25 = [("c1", 5.0), ("c2", 3.0), ("c3", 1.0)]
    dense = [("c2", 0.9), ("c4", 0.8), ("c1", 0.7)]
    scores = reciprocal_rank_fusion(bm25, dense)
    # c1 is rank1 in bm25 + rank3 in dense; c2 is rank2+rank1. Both should be top.
    top_two = sorted(scores, key=lambda k: -scores[k])[:2]
    assert set(top_two) == {"c1", "c2"}


# --- hybrid retriever end-to-end ----------------------------------------

def _build_retriever(chunks):
    router = ModelRouter(RouterConfig(live=False))
    ret = HybridRetriever(bm25_index=BM25Index(), vector_store=VectorStore(), router=router, rerank_enabled=False)
    ret.index_chunks(chunks)
    return ret


def test_hybrid_retriever_returns_relevant_within_budget():
    doc = _doc_with_two_sections()
    chunks = [
        _chunk(doc, "ck1", "变量是一个存储位置的名称。每个变量都有类型。", 1, ("3 变量与类型",), ["b2"]),
        _chunk(doc, "ck2", "引用变量保存的是对象的地址，而不是对象本身。", 3, ("4 引用与对象",), ["b5"]),
        _chunk(doc, "ck3", "== 比较引用是否指向同一个对象，equals 比较内容。", 4, ("4 引用与对象",), ["b6"]),
    ]
    ret = _build_retriever(chunks)
    hits = ret.retrieve("== 和 equals 有什么区别", top_k=3, context_budget=2)
    assert len(hits) <= 2  # context budget respected
    # The equality chunk should be in the result set.
    ids = {h.chunk.chunk_id for h in hits}
    assert "ck3" in ids


def test_hybrid_retriever_uses_ascii_concept_to_drop_unrelated_chapters():
    doc = _doc_with_two_sections()
    chunks = [
        _chunk(doc, "ck1", "改进版 Bitmap 目前仍不支持动态扩容。", 487, ("向量",), ["b1"]),
        _chunk(doc, "ck2", "KMP 算法还有一个改进版本。", 338, ("字符串",), ["b2"]),
        _chunk(doc, "ck3", "快速排序的改进版本如下。", 358, ("排序",), ["b3"]),
    ]
    hits = _build_retriever(chunks).retrieve(
        "改进版 bitmap 目前不支持什么功能", top_k=3, context_budget=3,
    )
    assert [hit.chunk.chunk_id for hit in hits] == ["ck1"]


def test_hybrid_retriever_allowlist_filters():
    doc = _doc_with_two_sections()
    chunks = [
        _chunk(doc, "ck1", "变量是存储位置的名称", 1, ("3 变量与类型",), ["b2"]),
        _chunk(doc, "ck2", "引用是对象的地址", 3, ("4 引用与对象",), ["b5"]),
    ]
    ret = _build_retriever(chunks)
    # Only ck1 is allowed.
    hits = ret.retrieve("变量", top_k=3, context_budget=3, allow_chunk_ids={"ck1"})
    assert all(h.chunk.chunk_id == "ck1" for h in hits)
    assert len(hits) == 1


def test_allowlist_is_applied_before_top_k_ranking():
    """A current-page chunk must not be crowded out by global top results."""
    doc = _doc_with_two_sections()
    global_chunks = [
        _chunk(doc, f"global-{i}", "时间复杂度 时间复杂度", 2,
               ("其他页",), ["b3"])
        for i in range(20)
    ]
    current = _chunk(
        doc, "current-page", "本页介绍复杂度下界以及数组访问", 50,
        ("当前页",), ["b2"],
    )
    ret = _build_retriever([*global_chunks, current])

    hits = ret.retrieve(
        "时间复杂度是什么", top_k=3, context_budget=3,
        allow_chunk_ids={"current-page"},
    )

    assert [hit.chunk.chunk_id for hit in hits] == ["current-page"]


# --- citation validator --------------------------------------------------

def test_citation_valid_pass():
    doc = _doc_with_two_sections()
    c = _chunk(doc, "ck1", "变量是一个存储位置的名称。", 1, ("3 变量与类型",), ["b2"])
    v = CitationValidator({"ck1": c}, {"book1"})
    report = v.validate(
        citations=[{"chunk_id": "ck1", "quote": "变量是一个存储位置的名称。", "page": "1"}],
        context_chunk_ids=["ck1"],
    )
    assert report.ok is True


def test_citation_rejects_quote_not_in_chunk():
    doc = _doc_with_two_sections()
    c = _chunk(doc, "ck1", "变量是一个存储位置的名称。", 1, ("3 变量与类型",), ["b2"])
    v = CitationValidator({"ck1": c}, {"book1"})
    report = v.validate(
        citations=[{"chunk_id": "ck1", "quote": "这段话不存在于chunk中", "page": "1"}],
        context_chunk_ids=["ck1"],
    )
    assert report.ok is False
    assert "not found" in report.checks[0].reason


def test_citation_rejects_chunk_not_in_context():
    doc = _doc_with_two_sections()
    c = _chunk(doc, "ck1", "变量是一个存储位置的名称。", 1, ("3 变量与类型",), ["b2"])
    v = CitationValidator({"ck1": c}, {"book1"})
    report = v.validate(
        citations=[{"chunk_id": "ck1", "quote": "变量是一个存储位置的名称。", "page": "1"}],
        context_chunk_ids=[],  # model was not given this chunk
    )
    assert report.ok is False
    assert "not in the provided context" in report.checks[0].reason


def test_citation_rejects_wrong_book():
    doc = _doc_with_two_sections()
    c = _chunk(doc, "ck1", "变量是一个存储位置的名称。", 1, ("3 变量与类型",), ["b2"])
    v = CitationValidator({"ck1": c}, {"other_book"})
    report = v.validate(
        citations=[{"chunk_id": "ck1", "quote": "变量是一个存储位置的名称。", "page": "1"}],
        context_chunk_ids=["ck1"],
    )
    assert report.ok is False
    assert "not in allowed books" in report.checks[0].reason


# --- end-to-end through the fallback parser ------------------------------

def test_parse_then_chunk_pipeline():
    mini_pdf = (
        b"%PDF-1.4 1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj "
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj "
        b"3 0 obj<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>endobj "
        b"4 0 obj<< /Length 80 >>stream\nBT /F1 12 Tf 72 700 Td (3.1 Variables) Tj "
        b"0 -14 Td (A variable names a storage location.) Tj ET\nendstream endobj"
    )
    p = PlainPdfFallback()
    doc = p.parse(mini_pdf, FileMetadata("mini.pdf"), ParseOptions(document_id="d1"))
    chunker = Chunker(target_tokens=8, overlap_tokens=2)
    chunks = chunker.chunk(doc, "book1")
    assert len(chunks) >= 1
    assert all(c.book_id == "book1" for c in chunks)
