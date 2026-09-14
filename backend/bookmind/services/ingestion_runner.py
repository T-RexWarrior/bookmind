"""Ingestion runner — drives one ingestion job through its stages with persisted
checkpoints (PRODUCTIZATION M3, ARCHITECTURE §11).

This wraps :class:`~bookmind.jobs.worker.IngestionWorker`'s stage logic so that:

* each stage writes a checkpoint to :class:`~bookmind.services.job_service.JobService`
  (so progress survives restart and is reported via SSE);
* the parsed document is persisted to ``{data_dir}/parsed/{source_hash}/
  {parser_version}/document.json`` so a duplicate upload reuses it (§9.4);
* the chunks are persisted to ``{data_dir}/indexes/{book_id}/chunks.json`` so
  the retriever can be rebuilt after a restart without re-parsing.

The runner runs in a single background worker thread (see
``api/app.py`` lifespan); it is synchronous and blocking, which is fine off
the event loop. A cancelled job (``JobState.CANCELLED``) is skipped.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings, get_settings
from ..jobs.job_store import IngestionJob, JobStage, JobState
from ..llm.router import ModelRouter
from ..retrieval.chunk import DocumentChunk
from ..retrieval.chunking import CHUNKER_VERSION, Chunker, scope_chunks_to_book
from ..retrieval.fusion import HybridRetriever
from ..retrieval.parsers import FileMetadata, ParseOptions
from ..storage.protocols import Repository
from .job_service import JobService

log = logging.getLogger("bookmind.ingestion")


@dataclass
class RunResult:
    job: IngestionJob
    chunks: list[DocumentChunk]
    reused: bool


class IngestionRunner:
    """Run one ingestion job to completion, persisting artifacts and checkpoints."""

    def __init__(
        self,
        repo: Repository,
        router: ModelRouter,
        jobs: JobService,
        settings: Settings | None = None,
        *,
        parsers=None,
    ) -> None:
        self.repo = repo
        self.router = router
        self.jobs = jobs
        self.settings = settings or get_settings()
        from ..retrieval.parsers import MinerUParser, PlainPdfFallback, PyPdfParser, RapidOcrParser
        self.parsers = parsers or [MinerUParser(), PyPdfParser(), RapidOcrParser(), PlainPdfFallback()]
        self._chunker = Chunker()

    # --- entry point -------------------------------------------------------

    def run_job(self, job_id: str) -> RunResult | None:
        """Drive ``job_id`` from its current stage to DONE. Returns None if the
        job was cancelled or is missing."""
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.state == JobState.CANCELLED:
            return None
        # Repeated clicks can leave several queued jobs for the same source.
        # Once one of them has completed this project's real graph, finish the
        # duplicates immediately instead of parsing the same textbook again.
        concepts = self.repo.concepts_for_book(job.book_id)
        graph_is_ready = bool(concepts) and not (
            job.book_id != "demo_java_core" and any(c.source == "GOLD" for c in concepts)
        )
        completed_sibling = any(
            candidate.job_id != job.job_id
            and candidate.book_id == job.book_id
            and candidate.state == JobState.SUCCEEDED
            for candidate in self.jobs.jobs_for_project(job.project_id)
        )
        if graph_is_ready and completed_sibling:
            job.stage = JobStage.DONE
            job.progress = 1.0
            job.state = JobState.SUCCEEDED
            job.error = ""
            self.jobs.update(job)
            self.jobs.emit(job.job_id, "run_completed", {"book_id": job.book_id, "reused": True})
            return RunResult(job=job, chunks=[], reused=True)
        # Read the stored source PDF.
        from .upload_service import UploadService
        upload = UploadService(self.settings)
        # The book's owner is the project owner.
        proj = self.repo.assert_project_owned_by(job.project_id, _owner_of(self.repo, job))
        source_path = upload.path_for(proj.learner_id, job.book_id)
        if not source_path.is_file():
            job.state = JobState.FAILED
            job.error = "源文件已丢失，请重新上传该资料。"
            self.jobs.update(job)
            self.jobs.emit(job_id, "run_failed", {"error": job.error})
            return None
        source = source_path.read_bytes()
        return self._run(job, source)

    def _run(self, job: IngestionJob, source: bytes) -> RunResult:
        job.state = JobState.RUNNING
        job.attempt += 1
        self.jobs.update(job)
        self.jobs.emit(job.job_id, "run_started", {"book_id": job.book_id})
        reused = True

        try:
            # Stage: PARSING (idempotent on source_hash + parser_version).
            job.stage = JobStage.PARSING
            job.progress = max(job.progress, 0.10)
            self.jobs.update(job)
            self.jobs.emit(job.job_id, "tool_started", {"tool": "parsing"})
            doc = self._parse(job, source)
            self.repo.update_source_metadata(
                job.book_id,
                parser_version=job.parser_version,
                page_count=len(doc.pages),
                section_count=len(doc.sections),
                outline=[
                    {
                        "title": section.title,
                        "page": section.physical_page,
                        "path": list(section.section_path),
                    }
                    for section in doc.sections[:300]
                    if section.title.strip()
                ],
            )
            reused = reused and doc is not None and _doc_has_text(doc)
            job.stage = JobStage.CHUNKING
            job.progress = 0.30
            self.jobs.update(job)
            self.jobs.emit(job.job_id, "tool_completed", {"tool": "parsing"})

            if not _doc_has_text(doc):
                # Empty blocks → scanned / encrypted / compressed. Surface the
                # parser's precise health_warning if it gave one, else the
                # generic scanned/encrypted copy (§5.2).
                warning = getattr(doc, "health_warning", None) or ""
                if warning.startswith("encrypted"):
                    job.error = "该 PDF 已加密，请提供密码或解密后重新上传。"
                elif warning.startswith("compressed"):
                    job.error = "该 PDF 的压缩内容无法解析，本地 OCR 也未识别到可用文字。"
                elif warning.startswith("scanned"):
                    job.error = "该 PDF 已自动尝试本地中文 OCR，但未识别到可用文字。"
                else:
                    job.error = "未检测到可用文字，该 PDF 可能是扫描件或加密文件。"
                job.state = JobState.RETRYABLE_FAILED
                self.jobs.update(job)
                self.jobs.emit(job.job_id, "run_failed", {"error": job.error, "code": "FILE_SCANNED"})
                return RunResult(job=job, chunks=[], reused=False)

            # Stage: CHUNKING + INDEXING (one user-facing stage).
            self.jobs.emit(job.job_id, "tool_started", {"tool": "indexing"})
            chunks = self._chunk(job, doc)
            self._persist_chunks(job.book_id, chunks)
            # Build / refresh the project retriever from the persisted chunks.
            self._index_into_retriever(job, chunks)
            job.chunker_version = CHUNKER_VERSION
            job.stage = JobStage.GRAPH
            job.progress = 0.90
            self.jobs.update(job)
            self.jobs.emit(job.job_id, "tool_completed", {"tool": "indexing"})

            # Stage: GRAPH — build the knowledge structure so the book can be
            # quizzed. P0-03: a book that shows "准备完成" but has 0 concepts
            # cannot enter the 出题—验证 loop. We seed concepts from the Book
            # Mapper (offline: the gold skeleton + section fallbacks) so at
            # least one real concept is available for "考考我".
            self.jobs.emit(job.job_id, "tool_started", {"tool": "graph"})
            self._build_graph(job, doc, chunks)
            self.jobs.emit(job.job_id, "tool_completed", {"tool": "graph"})

            job.stage = JobStage.DONE
            job.progress = 1.0
            job.state = JobState.SUCCEEDED
            self.jobs.update(job)
            self.jobs.emit(job.job_id, "run_completed", {"book_id": job.book_id, "chunks": len(chunks)})
            return RunResult(job=job, chunks=chunks, reused=reused)

        except Exception as exc:  # noqa: BLE001 — surface any stage failure
            log.exception("Ingestion failed for job %s", job.job_id)
            job.state = JobState.RETRYABLE_FAILED
            job.error = "资料处理没有完成，请重试；如果问题持续，请更换或重新导出 PDF。"
            self.jobs.update(job)
            self.jobs.emit(job.job_id, "run_failed", {"error": job.error})
            return RunResult(job=job, chunks=[], reused=False)

    # --- stages ------------------------------------------------------------

    def _parse(self, job: IngestionJob, source: bytes):
        """Try every available parser in quality order until one yields text."""
        from ..retrieval.parsed_document import ParsedDocument

        meta = FileMetadata(job.filename)
        candidates = sorted(
            ((parser.supports(meta), index, parser) for index, parser in enumerate(self.parsers)),
            key=lambda item: (-item[0], item[1]),
        )
        candidates = [parser for confidence, _, parser in candidates if confidence > 0]
        if not candidates:
                raise RuntimeError(f"没有可用于 {job.filename} 的资料解析器")

        last_empty = None
        errors: list[str] = []
        for parser in candidates:
            job.parser = parser.name
            job.parser_version = parser.version
            self.jobs.update(job)
            doc_path = self._parsed_path(job.source_hash, parser.version)
            if doc_path.is_file():
                try:
                    doc = ParsedDocument.model_validate_json(doc_path.read_text("utf-8"))
                    if _doc_has_text(doc):
                        return doc
                    last_empty = doc
                    continue
                except Exception:  # noqa: BLE001 — corrupt cache, re-parse
                    pass
            try:
                doc = parser.parse(
                    source,
                    meta,
                    ParseOptions(document_id=f"doc-{job.source_hash[:12]}"),
                )
                doc_path.parent.mkdir(parents=True, exist_ok=True)
                doc_path.write_text(doc.model_dump_json(), encoding="utf-8")
                if _doc_has_text(doc):
                    return doc
                last_empty = doc
            except Exception as exc:  # noqa: BLE001 — continue to the next provider
                errors.append(f"{parser.name}: {exc}")
                log.warning("parser %s failed for %s: %s", parser.name, job.filename, exc)

        if last_empty is not None:
            return last_empty
        raise RuntimeError("；".join(errors) or "所有 PDF 解析器均不可用")

    def _chunk(self, job: IngestionJob, doc) -> list[DocumentChunk]:
        import hashlib
        ckey = f"{job.source_hash}|{CHUNKER_VERSION}"
        # Hash the key for the filename — the raw key contains "|" which is
        # illegal in Windows filenames.
        safe = hashlib.sha256(ckey.encode()).hexdigest()[:24]
        chunk_path = self._chunks_cache_path(safe)
        if chunk_path.is_file():
            try:
                cached = [
                    DocumentChunk.model_validate(c)
                    for c in json.loads(chunk_path.read_text("utf-8"))
                ]
                return scope_chunks_to_book(cached, job.book_id)
            except Exception:  # noqa: BLE001
                pass
        cached_chunks = self._chunker.chunk(doc, job.book_id)
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_path.write_text(
            json.dumps([c.model_dump(mode="json") for c in cached_chunks], ensure_ascii=False),
            encoding="utf-8",
        )
        return scope_chunks_to_book(cached_chunks, job.book_id)

    def _index_into_retriever(self, job: IngestionJob, chunks: list[DocumentChunk]) -> None:
        """Index chunks into the project retriever (creating one if absent)."""
        from ..retrieval.bm25 import BM25Index
        from ..retrieval.vector import VectorStore
        ret = self.repo.get_retriever(job.project_id) or HybridRetriever(
            bm25_index=BM25Index(), vector_store=VectorStore(),
            router=self.router, rerank_enabled=False,
        )
        existing = set(ret.chunks.keys())
        new_chunks = [c for c in chunks if c.chunk_id not in existing]
        if new_chunks:
            ret.index_chunks(new_chunks)
        self.repo.set_retriever(job.project_id, ret)
        self.repo.add_chunks(job.book_id, chunks)

    def _build_graph(self, job: IngestionJob, doc, chunks: list[DocumentChunk]) -> None:
        """Build the knowledge structure for the book so it can be quizzed.

        P0-03: without concepts ``request_task`` has nothing to probe. We run
        the Book Mapping use case; offline it falls back to the gold skeleton +
        per-section heuristic concepts, giving at least one in-scope concept.
        The book must already have chunks indexed. Idempotent: if the book
        already has concepts, this is a no-op.
        """
        existing = self.repo.concepts_for_book(job.book_id)
        contaminated = (
            job.book_id != "demo_java_core"
            and any(c.source == "GOLD" for c in existing)
        )
        if existing and not contaminated:
            return  # already mapped by the real-book pipeline
        if contaminated:
            log.warning(
                "rebuilding legacy demo-contaminated graph for real book %s",
                job.book_id,
            )
        try:
            from ..llm.router import RouterConfig
            from .book_mapping import BookMappingService
            # Ingestion must be reliably bounded. A full textbook can contain
            # hundreds of sections; making one network call per section here
            # used to hold the only worker indefinitely and leave later files
            # at 5%. The deterministic mapper still builds a source-grounded
            # graph, while richer live explanations remain available in chat.
            svc = BookMappingService(self.repo, ModelRouter(RouterConfig(live=False)))
            report = svc.map_book(
                project_id=job.project_id, learner_id=_owner_of(self.repo, job),
                book_id=job.book_id, parsed_document=doc, chunks=chunks,
                graph_key=f"{job.source_hash}:{job.parser_version}:{CHUNKER_VERSION}",
            )
            if report.total_concepts == 0:
                raise RuntimeError("未能从资料正文抽取出任何概念，请检查文字层或模型配置。")
        except Exception as exc:  # noqa: BLE001 — graph failure is fatal
            log.warning("graph build failed for %s: %s", job.book_id, exc)
            raise

    # --- artifact paths ----------------------------------------------------

    def _parsed_path(self, source_hash: str, parser_version: str) -> Path:
        return Path(self.settings.data_dir) / "parsed" / source_hash / parser_version / "document.json"

    def _chunks_cache_path(self, ckey: str) -> Path:
        return Path(self.settings.data_dir) / "chunks_cache" / f"{ckey}.json"

    def _persist_chunks(self, book_id: str, chunks: list[DocumentChunk]) -> None:
        """Persist chunks for this book so the retriever can be rebuilt post-restart."""
        path = Path(self.settings.data_dir) / "indexes" / book_id / "chunks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([c.model_dump(mode="json") for c in chunks], ensure_ascii=False), encoding="utf-8")

    # --- rebuild (post-restart) -------------------------------------------

    def rebuild_retriever_if_needed(self, project_id: str) -> None:
        """If the project has no in-memory retriever but has persisted chunks on
        disk, rebuild it from those chunks (offline-hash vectors are instant)."""
        if self.repo.get_retriever(project_id) is not None:
            return
        book_ids = self.repo.allowed_book_ids(project_id)
        if not book_ids:
            return
        all_chunks: list[DocumentChunk] = []
        for bid in book_ids:
            path = Path(self.settings.data_dir) / "indexes" / bid / "chunks.json"
            if path.is_file():
                try:
                    cached = [
                        DocumentChunk.model_validate(c)
                        for c in json.loads(path.read_text("utf-8"))
                    ]
                    all_chunks.extend(scope_chunks_to_book(cached, bid))
                except Exception:  # noqa: BLE001
                    pass
        if not all_chunks:
            return
        from ..retrieval.bm25 import BM25Index
        from ..retrieval.vector import VectorStore
        ret = HybridRetriever(bm25_index=BM25Index(), vector_store=VectorStore(),
                              router=self.router, rerank_enabled=False)
        ret.index_chunks(all_chunks)
        self.repo.set_retriever(project_id, ret)
        for bid in book_ids:
            self.repo.add_chunks(bid, [c for c in all_chunks if c.book_id == bid])


def _doc_has_text(doc) -> bool:
    return bool(doc and doc.blocks)


def _owner_of(repo: Repository, job: IngestionJob) -> str:
    """Return the learner_id owning the job's project (for file lookup)."""
    project = repo.get_project(job.project_id)
    return project.learner_id if project else ""
