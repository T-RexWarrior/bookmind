"""L1 tests: ingestion worker & job store — ARCHITECTURE.md §11."""

from __future__ import annotations

import pytest

from bookmind.jobs import (
    IngestionJob,
    IngestionWorker,
    JobState,
    JobStore,
    chunk_key,
    index_key,
    parse_key,
)
from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.retrieval.parsers import PlainPdfFallback


_MINI_PDF = (
    b"%PDF-1.4 1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj "
    b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj "
    b"3 0 obj<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>endobj "
    b"4 0 obj<< /Length 80 >>stream\nBT /F1 12 Tf 72 700 Td (3.1 Variables) Tj "
    b"0 -14 Td (A variable names a storage location.) Tj ET\nendstream endobj"
)


def _worker():
    store = JobStore()
    router = ModelRouter(RouterConfig(live=False))
    return store, IngestionWorker(store, router, parsers=[PlainPdfFallback()])


def test_ingest_succeeds_and_indexes():
    store, worker = _worker()
    res = worker.ingest(
        project_id="p1", book_id="b1", source=_MINI_PDF, filename="book.pdf",
    )
    assert res.job.state == JobState.SUCCEEDED
    assert res.job.progress == 1.0
    assert res.doc is not None and res.doc.blocks
    assert res.chunks
    assert res.retriever is not None
    assert len(res.retriever.chunks) == len(res.chunks)
    assert res.reused is False


def test_ingest_same_file_is_idempotent():
    store, worker = _worker()
    r1 = worker.ingest(project_id="p1", book_id="b1", source=_MINI_PDF, filename="book.pdf")
    # Second ingestion of the same bytes with the SAME retriever: every stage
    # is a cache hit (parse + chunk in the JobStore, chunks already indexed).
    r2 = worker.ingest(
        project_id="p1", book_id="b1", source=_MINI_PDF, filename="book.pdf",
        retriever=r1.retriever,
    )
    assert r2.job.state == JobState.SUCCEEDED
    assert r2.reused is True  # all stages cached
    assert len(r2.retriever.chunks) == len(r1.retriever.chunks)


def test_ingest_reuses_parse_and_chunk_even_with_fresh_retriever():
    # Even with a brand-new retriever, the parse + chunk caches still hit —
    # only the index stage re-runs. This proves "相同文件不重复处理" for the
    # expensive parsing/chunking derivation.
    store, worker = _worker()
    r1 = worker.ingest(project_id="p1", book_id="b1", source=_MINI_PDF, filename="book.pdf")
    pkey = r1.job.parse_key
    ckey = r1.job.chunk_key
    assert store.has_parse(pkey)
    assert store.has_chunks(ckey)
    r2 = worker.ingest(project_id="p1", book_id="b1", source=_MINI_PDF, filename="book.pdf")
    # Same parsed doc and chunks object identity from the cache.
    assert r2.doc is store.get_parse(pkey)
    assert r2.chunks is store.get_chunks(ckey)


def test_recover_running_resets_to_pending():
    store, worker = _worker()
    job = IngestionJob(job_id="j1", project_id="p1", book_id="b1",
                      source_hash="h", filename="x.pdf", state=JobState.RUNNING)
    store.submit(job)
    recovered = store.recover_running()
    assert len(recovered) == 1
    assert recovered[0].state == JobState.PENDING


def test_derivation_keys_are_deterministic():
    pkey = parse_key("abc", "v1")
    assert pkey == parse_key("abc", "v1")
    assert pkey != parse_key("abc", "v2")  # parser version matters
    ckey = chunk_key(pkey, "chunker_v1")
    assert ckey != chunk_key(pkey, "chunker_v2")
    ikey = index_key(ckey, "qwen3-embedding", 4096)
    assert ikey != index_key(ckey, "other-model", 4096)  # model matters
    assert ikey != index_key(ckey, "qwen3-embedding", 768)  # dim matters


def test_jobs_scoped_to_project():
    store, worker = _worker()
    worker.ingest(project_id="p1", book_id="b1", source=_MINI_PDF, filename="a.pdf")
    worker.ingest(project_id="p2", book_id="b2", source=_MINI_PDF, filename="b.pdf")
    assert len(store.jobs_for_project("p1")) == 1
    assert len(store.jobs_for_project("p2")) == 1
    assert len(store.jobs_for_project("p3")) == 0


def test_parse_failure_marks_retryable():
    store, worker = _worker()
    # Feed bytes that no parser can meaningfully parse but that won't crash —
    # PlainPdfFallback returns empty blocks, not an exception, so we simulate a
    # parser error by passing non-PDF bytes through a worker whose only parser
    # supports nothing.
    from bookmind.retrieval.parsers.base import DocumentParser, FileMetadata, ParseOptions
    from bookmind.retrieval.parsed_document import ParsedDocument

    class BrokenParser(DocumentParser):
        name = "broken"
        version = "v0"
        def supports(self, meta): return 1.0
        def parse(self, source, meta, options=None):
            raise RuntimeError("boom")

    store2 = JobStore()
    router = ModelRouter(RouterConfig(live=False))
    w = IngestionWorker(store2, router, parsers=[BrokenParser()])
    with pytest.raises(RuntimeError):
        w.ingest(project_id="p1", book_id="b1", source=b"x", filename="x.pdf")
    job = store2.all_jobs()[0]
    assert job.state == JobState.RETRYABLE_FAILED
    assert "boom" in job.error


def test_empty_primary_parser_falls_back_to_next_provider():
    from bookmind.retrieval.parsed_document import ParsedDocument
    from bookmind.retrieval.parsers.base import DocumentParser

    class EmptyParser(DocumentParser):
        name = "empty"
        version = "v1"

        def supports(self, meta):
            return 1.0

        def parse(self, source, meta, options=None):
            return ParsedDocument(
                document_id="empty-doc",
                source_file=meta.filename,
                source_hash="empty-hash",
                parser=self.name,
                parser_version=self.version,
                health_warning="scanned: no text layer",
            )

    store = JobStore()
    router = ModelRouter(RouterConfig(live=False))
    worker = IngestionWorker(store, router, parsers=[EmptyParser(), PlainPdfFallback()])
    result = worker.ingest(project_id="p1", book_id="b1", source=_MINI_PDF, filename="book.pdf")

    assert result.job.state == JobState.SUCCEEDED
    assert result.job.parser == "plain_pdf"
    assert result.doc is not None and result.doc.blocks


def test_source_hash_cached_chunks_are_rebound_to_current_book():
    """A shared derivation cache must never retain another owner's book id."""
    from bookmind.retrieval.chunking import Chunker, scope_chunks_to_book
    from bookmind.retrieval.parsers import FileMetadata, ParseOptions

    doc = PlainPdfFallback().parse(
        _MINI_PDF, FileMetadata("book.pdf"), ParseOptions(document_id="shared-doc"),
    )
    original = Chunker(target_tokens=8, overlap_tokens=2).chunk(doc, "book_owner_a")
    rebound = scope_chunks_to_book(original, "book_owner_b")

    assert rebound
    assert all(chunk.book_id == "book_owner_b" for chunk in rebound)
    assert all(chunk.chunk_id.startswith("book_owner_b:") for chunk in rebound)
    assert all(chunk.source_ref.chunk_id == chunk.chunk_id for chunk in rebound)
    assert all("book_owner_a:" not in chunk.chunk_id for chunk in rebound)

    rebound_again = scope_chunks_to_book(rebound, "book_owner_b")
    assert [chunk.chunk_id for chunk in rebound_again] == [
        chunk.chunk_id for chunk in rebound
    ]

    transferred = scope_chunks_to_book(rebound, "book_owner_c")
    assert all(chunk.chunk_id.startswith("book_owner_c:") for chunk in transferred)
    assert all("book_owner_b:" not in chunk.chunk_id for chunk in transferred)
