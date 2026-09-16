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
import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings, get_settings
from ..jobs.job_store import (
    IngestionJob, JobStage, JobState, chunk_key as build_chunk_key,
    index_key as build_index_key, parse_key as build_parse_key,
)
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
        from ..retrieval.parsers import AdaptivePdfParser, PlainPdfFallback, PpStructureParser
        high_precision = None
        if self.settings.document_parser_url and self.settings.document_parser in {"auto", "ppstructure"}:
            high_precision = PpStructureParser(
                self.settings.document_parser_url,
                timeout=self.settings.document_parser_timeout,
            )
        self.parsers = parsers or [
            AdaptivePdfParser(high_precision=high_precision, batch_pages=self.settings.parse_batch_pages),
            PlainPdfFallback(),
        ]
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
        if graph_is_ready and completed_sibling and not job.force_reparse:
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
            job.checkpoint_stage = "读取页面"
            job.progress = max(job.progress, 0.10)
            self.jobs.update(job)
            self.jobs.emit(job.job_id, "tool_started", {"tool": "parsing"})
            doc = self._parse(job, source)
            job.quality_summary = doc.quality_summary
            job.warnings = doc.warnings
            self.repo.update_source_metadata(
                job.book_id,
                parser_version=job.parser_version,
                page_count=len(doc.pages),
                section_count=len(doc.sections),
                outline=_outline_payload(doc),
            )
            reused = reused and doc is not None and _doc_has_text(doc)
            job.stage = JobStage.CHUNKING
            job.checkpoint_stage = "生成切块"
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
            # Build / refresh the project retriever from the persisted chunks.
            job.checkpoint_stage = "生成关键词和向量索引"
            self.jobs.update(job)
            self._index_into_retriever(job, chunks)
            job.chunker_version = CHUNKER_VERSION
            job.stage = JobStage.GRAPH
            job.checkpoint_stage = "整理知识范围"
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
            job.checkpoint_stage = "发布完成"
            job.progress = 1.0
            job.state = JobState.SUCCEEDED
            self.jobs.update(job)
            self.jobs.emit(job.job_id, "run_completed", {"book_id": job.book_id, "chunks": len(chunks)})
            return RunResult(job=job, chunks=chunks, reused=reused)

        except Exception as exc:  # noqa: BLE001 — surface any stage failure
            log.exception("Ingestion failed for job %s", job.job_id)
            if self._cancelled(job.job_id):
                job.state = JobState.CANCELLED
                job.error = ""
                self.jobs.update(job)
                self.jobs.emit(job.job_id, "run_cancelled", {})
                return RunResult(job=job, chunks=[], reused=False)
            job.state = JobState.RETRYABLE_FAILED
            job.error = "资料处理没有完成，请重试；如果问题持续，请更换或重新导出 PDF。"
            self.jobs.update(job)
            self.jobs.emit(job.job_id, "run_failed", {"error": job.error})
            return RunResult(job=job, chunks=[], reused=False)

    # --- stages ------------------------------------------------------------

    def _parse(self, job: IngestionJob, source: bytes):
        """Try every available parser in quality order until one yields text."""
        from ..retrieval.parsed_document import ParsedDocument

        meta = FileMetadata(job.filename, num_bytes=len(source))
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
            job.parser_mode = self.settings.document_parser
            self.jobs.update(job)
            fingerprint = getattr(parser, "config_fingerprint", "default")
            job.parse_key = build_parse_key(
                job.source_hash, f"{parser.version}|{fingerprint}",
            )
            self.jobs.update(job)
            doc_path = self._parsed_path(job.source_hash, parser.version, str(fingerprint))
            if doc_path.is_file() and not job.force_reparse:
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
                    ParseOptions(
                        document_id=f"doc-{job.source_hash[:12]}",
                        on_page=lambda done, total, mode: self._page_checkpoint(job, done, total, mode),
                        is_cancelled=lambda: self._cancelled(job.job_id),
                        force_reparse=job.force_reparse,
                    ),
                )
                doc_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = doc_path.with_suffix(".tmp")
                tmp_path.write_text(doc.model_dump_json(), encoding="utf-8")
                tmp_path.replace(doc_path)
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
        ckey = (
            f"{job.source_hash}|{doc.pipeline_version}|{doc.parser_version}|"
            f"{doc.config_fingerprint}|{CHUNKER_VERSION}"
        )
        job.chunk_key = build_chunk_key(job.parse_key or job.source_hash, CHUNKER_VERSION)
        self.jobs.update(job)
        # Hash the key for the filename — the raw key contains "|" which is
        # illegal in Windows filenames.
        safe = hashlib.sha256(ckey.encode()).hexdigest()[:24]
        chunk_path = self._chunks_cache_path(safe)
        if chunk_path.is_file() and not job.force_reparse:
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
        """Build a candidate index and swap it in only after atomic disk publish."""
        from ..retrieval.bm25 import BM25Index
        from ..retrieval.vector import VectorStore
        previous = self.repo.get_retriever(job.project_id)
        ret = HybridRetriever(
            bm25_index=BM25Index(), vector_store=VectorStore(),
            router=self.router, rerank_enabled=True,
        )
        others = [
            chunk for chunk in self.repo.chunks_for_project(job.project_id)
            if chunk.book_id != job.book_id
        ]
        for chunk in others:
            indexed = previous.chunks.get(chunk.chunk_id, chunk) if previous else chunk
            ret.chunks[indexed.chunk_id] = indexed
            ret.bm25_index.add(indexed)
            vector = previous.vector_store.vector_for(indexed.chunk_id) if previous else None
            if vector is not None and indexed.embedding_space:
                ret.vector_store.add(indexed, vector)
        embedding_result = ret.index_chunks(chunks, allow_embedding_fallback=False)
        if embedding_result is not None:
            job.embedding_model = embedding_result.model if not embedding_result.fallback else ""
            job.embedding_dim = embedding_result.dim if not embedding_result.fallback else 0
            if embedding_result.fallback:
                if embedding_result.model == "disabled":
                    job.warnings = [
                        *job.warnings,
                        "DeepSeek 官方暂未提供 Embedding 接口；当前使用关键词召回、DeepSeek 检索词扩展与候选重排。",
                    ]
                else:
                    job.warnings = [
                        *job.warnings,
                        "Embedding 服务不可用，已先发布关键词索引；向量索引将在服务恢复后补建。",
                    ]
        job.index_key = build_index_key(
            job.chunk_key or job.source_hash,
            job.embedding_model or "keyword-only",
            job.embedding_dim,
            index_version=2,
        )
        from ..retrieval.persistent_index import publish_index
        indexed = [ret.chunks.get(chunk.chunk_id, chunk) for chunk in chunks]
        vectors = [ret.vector_store.vector_for(chunk.chunk_id) for chunk in indexed]
        complete_vectors = None if any(vector is None for vector in vectors) else vectors
        publish_index(
            Path(self.settings.data_dir) / "indexes" / job.book_id,
            indexed, vectors=complete_vectors, embedding_model=job.embedding_model,
        )
        # Only now do readers stop seeing the previous index.
        self.repo.replace_chunks(job.book_id, chunks)
        self.repo.set_retriever(job.project_id, ret)
        self.jobs.update(job)

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
        if existing and not contaminated and not job.force_reparse:
            return  # already mapped by the real-book pipeline
        if job.force_reparse:
            log.info("force-rebuilding graph for %s", job.book_id)
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

    def _parsed_path(self, source_hash: str, parser_version: str, fingerprint: str = "default") -> Path:
        return Path(self.settings.data_dir) / "parsed" / source_hash / parser_version / fingerprint / "document.json"

    def _chunks_cache_path(self, ckey: str) -> Path:
        return Path(self.settings.data_dir) / "chunks_cache" / f"{ckey}.json"

    def _persist_chunks(self, book_id: str, chunks: list[DocumentChunk]) -> None:
        """Persist chunks for this book so the retriever can be rebuilt post-restart."""
        path = Path(self.settings.data_dir) / "indexes" / book_id / "chunks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([c.model_dump(mode="json") for c in chunks], ensure_ascii=False), encoding="utf-8")

    def _cancelled(self, job_id: str) -> bool:
        current = self.jobs.get(job_id)
        return current is None or current.state == JobState.CANCELLED

    def _page_checkpoint(self, job: IngestionJob, done: int, total: int, mode: str) -> None:
        if self._cancelled(job.job_id):
            raise RuntimeError("解析已取消")
        job.pages_done = max(job.pages_done, done)
        job.pages_total = max(job.pages_total, total)
        job.parser_mode = mode
        job.checkpoint_stage = f"解析第 {done}/{total} 页"
        # Parsing occupies 10%..30% of the visible progress bar.
        job.progress = max(job.progress, 0.10 + 0.20 * done / max(1, total))
        self.jobs.update(job)
        self.jobs.emit(job.job_id, "page_progress", {
            "pages_done": job.pages_done, "pages_total": job.pages_total,
            "parser_mode": mode, "checkpoint_stage": job.checkpoint_stage,
        })

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
                              router=self.router, rerank_enabled=True)
        for chunk in all_chunks:
            ret.chunks[chunk.chunk_id] = chunk
            ret.bm25_index.add(chunk)
        from ..retrieval.persistent_index import load_vectors
        by_id = {chunk.chunk_id: chunk for chunk in all_chunks}
        active_space = ""
        for bid in book_ids:
            ids, matrix, model = load_vectors(Path(self.settings.data_dir) / "indexes" / bid)
            if matrix is None or not model or (active_space and active_space != model):
                continue
            active_space = model
            for row_index, chunk_id in enumerate(ids):
                chunk = by_id.get(chunk_id)
                if chunk is None:
                    continue
                indexed = chunk.model_copy(update={"embedding_space": model})
                ret.chunks[chunk_id] = indexed
                ret.vector_store.add(indexed, matrix[row_index].tolist())
        self.repo.set_retriever(project_id, ret)
        for bid in book_ids:
            self.repo.add_chunks(bid, [c for c in all_chunks if c.book_id == bid])


def _doc_has_text(doc) -> bool:
    return bool(doc and doc.blocks)


def _owner_of(repo: Repository, job: IngestionJob) -> str:
    """Return the learner_id owning the job's project (for file lookup)."""
    project = repo.get_project(job.project_id)
    return project.learner_id if project else ""


def _outline_payload(doc) -> list[dict]:
    """Build a deduplicated, monotonic outline with page ranges/confidence."""
    items: list[dict] = []
    seen: set[tuple[str, int]] = set()
    page_quality = {page.physical_page: page.quality_score for page in doc.pages}
    for section in sorted(doc.sections, key=lambda item: item.physical_page):
        title = section.title.strip()
        key = ("".join(title.split()).casefold(), section.physical_page)
        if not title or key in seen:
            continue
        seen.add(key)
        items.append({
            "title": title, "page": section.physical_page,
            "path": list(section.section_path),
            "confidence": page_quality.get(section.physical_page),
        })
        if len(items) >= 300:
            break
    total_pages = len(doc.pages)
    for index, item in enumerate(items):
        next_page = items[index + 1]["page"] if index + 1 < len(items) else total_pages + 1
        item["page_end"] = max(item["page"], next_page - 1)
    return items
