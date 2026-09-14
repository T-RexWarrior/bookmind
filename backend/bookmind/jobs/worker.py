"""Ingestion worker — runs the parse → chunk → index pipeline (ARCHITECTURE §11).

Single worker, synchronous for the demo. Each stage writes a checkpoint to the
:class:`JobStore` so a resumed job continues from its last completed stage.
Idempotent caches mean a re-submission of the same file with the same parser/
chunker/embedding versions is a no-op (ROADMAP Phase 2: "相同文件不重复处理").
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from ..llm.router import ModelRouter
from ..retrieval.chunk import DocumentChunk
from ..retrieval.chunking import CHUNKER_VERSION, Chunker
from ..retrieval.fusion import HybridRetriever
from ..retrieval.parsed_document import ParsedDocument
from ..retrieval.parsers import FileMetadata, ParseOptions
from .job_store import (
    IngestionJob,
    JobStage,
    JobState,
    JobStore,
    chunk_key,
    index_key,
    parse_key,
)


@dataclass
class IngestionResult:
    job: IngestionJob
    doc: ParsedDocument | None
    chunks: list[DocumentChunk]
    retriever: HybridRetriever | None
    reused: bool  # True iff every stage was a cache hit


class IngestionWorker:
    """Drives one ingestion job through its stages."""

    def __init__(self, store: JobStore, router: ModelRouter, *, parsers=None) -> None:
        self.store = store
        self.router = router
        from ..retrieval.parsers import MinerUParser, PlainPdfFallback, PyPdfParser, RapidOcrParser
        self.parsers = parsers or [MinerUParser(), PyPdfParser(), RapidOcrParser(), PlainPdfFallback()]

    def ingest(
        self,
        *,
        project_id: str,
        book_id: str,
        source: bytes,
        filename: str,
        retriever: HybridRetriever | None = None,
    ) -> IngestionResult:
        """Run the full pipeline for one uploaded file.

        ``retriever`` is the project's HybridRetriever; if None a fresh one is
        built so the chunks get indexed. Returns the final job, doc, chunks and
        the retriever holding the index.
        """
        source_hash = _sha256(source)
        job = IngestionJob(
            job_id=f"job-{uuid.uuid4().hex[:10]}",
            project_id=project_id, book_id=book_id,
            source_hash=source_hash, filename=filename,
            parser=self.parsers[0].name,
        )
        self.store.submit(job)
        return self._run(job, source, retriever)

    def resume(self, job: IngestionJob, source: bytes, retriever: HybridRetriever | None = None) -> IngestionResult:
        """Resume an existing job from its current stage."""
        return self._run(job, source, retriever)

    # --- stage runner ------------------------------------------------------

    def _run(self, job: IngestionJob, source: bytes, retriever: HybridRetriever | None) -> IngestionResult:
        job.state = JobState.RUNNING
        job.attempt += 1
        job.touch()
        reused = True

        # Stage 1: parse (idempotent on parse_key).
        parser_stack_version = "+".join(parser.version for parser in self.parsers)
        pkey = parse_key(job.source_hash, parser_stack_version)
        job.parse_key = pkey
        doc = self.store.get_parse(pkey)
        if doc is None:
            doc = self._parse(job, source)
            self.store.put_parse(pkey, doc)
            reused = False
        else:
            job.parser = doc.parser
            job.parser_version = doc.parser_version
        job.stage = JobStage.CHUNKING
        job.progress = 0.33
        job.touch()

        # Stage 2: chunk (idempotent on chunk_key).
        ckey = chunk_key(pkey, CHUNKER_VERSION)
        job.chunk_key = ckey
        chunks = self.store.get_chunks(ckey)
        if chunks is None:
            chunks = Chunker().chunk(doc, job.book_id)
            self.store.put_chunks(ckey, chunks)
            reused = False
        job.chunker_version = CHUNKER_VERSION
        job.stage = JobStage.INDEXING
        job.progress = 0.66
        job.touch()

        # Stage 3: index (idempotent on index_key; embedding model/dim aware).
        ret = retriever or HybridRetriever(
            bm25_index=_new_bm25(), vector_store=_new_vector(), router=self.router, rerank_enabled=False,
        )
        # Only index chunks not already present.
        existing = set(ret.chunks.keys())
        new_chunks = [c for c in chunks if c.chunk_id not in existing]
        if new_chunks:
            ret.index_chunks(new_chunks)
            reused = False
        emb_space = ret.vector_store.embedding_space
        ikey = index_key(ckey, emb_space or "offline-hash-256", _infer_dim(ret))
        job.index_key = ikey
        job.embedding_model = emb_space
        job.embedding_dim = _infer_dim(ret)
        job.stage = JobStage.GRAPH
        job.progress = 0.9
        job.touch()

        # Stage 4 (graph) is a placeholder here — the Book Mapper fills it in
        # Phase 3. We mark the job SUCCEEDED once chunks are indexed.
        job.stage = JobStage.DONE
        job.progress = 1.0
        job.state = JobState.SUCCEEDED
        job.touch()
        return IngestionResult(job=job, doc=doc, chunks=chunks, retriever=ret, reused=reused)

    def _parse(self, job: IngestionJob, source: bytes) -> ParsedDocument:
        meta = FileMetadata(job.filename)
        candidates = sorted(
            ((parser.supports(meta), index, parser) for index, parser in enumerate(self.parsers)),
            key=lambda item: (-item[0], item[1]),
        )
        candidates = [parser for confidence, _, parser in candidates if confidence > 0]
        last_empty: ParsedDocument | None = None
        errors: list[str] = []
        try:
            for parser in candidates:
                job.parser = parser.name
                job.parser_version = parser.version
                job.touch()
                try:
                    doc = parser.parse(source, meta, ParseOptions(document_id=f"doc-{job.source_hash[:12]}"))
                except Exception as exc:  # noqa: BLE001 — provider fallback
                    errors.append(f"{parser.name}: {exc}")
                    continue
                if doc.blocks:
                    return doc
                last_empty = doc
            if last_empty is not None:
                job.error = last_empty.health_warning or "parser produced no blocks"
                return last_empty
            raise RuntimeError("；".join(errors) or f"no parser supports {meta.filename}")
        except Exception as e:
            job.error = str(e)
            job.state = JobState.RETRYABLE_FAILED
            job.touch()
            raise

    # --- recovery ----------------------------------------------------------

    def recover(self) -> list[IngestionJob]:
        return self.store.recover_running()


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _new_bm25():
    from ..retrieval.bm25 import BM25Index
    return BM25Index()


def _new_vector():
    from ..retrieval.vector import VectorStore
    return VectorStore()


def _infer_dim(ret: HybridRetriever) -> int:
    # The VectorStore does not expose dim directly; infer from one entry.
    for entry in ret.vector_store._vectors:  # noqa: SLF001
        return len(entry.vector)
    return 0
