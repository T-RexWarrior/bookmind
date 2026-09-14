"""Grounded source QA service — orchestrates retrieval and cited answers.

Wires retrieval → context → Tutor → citation validation, all under the
project/book scope. This is the service the HTTP API calls; it owns the
transactions and scope checks that Agents and the Engine must not bypass.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..agents.tutor import TutorAgent
from ..domain.enums import ActivityMode, InterventionPolicy
from ..jobs import IngestionWorker
from ..llm.router import ModelRouter
from ..retrieval.citation import CitationValidator
from ..retrieval.fusion import HybridRetriever, RetrievalHit
from ..services.context_builder import ContextBuilder, ContextRequest
from ..storage.protocols import Repository, ScopeError


@dataclass
class AskResult:
    answer_text: str
    citations: list[dict]
    grounded: bool
    chunk_ids: list[str]
    reason: str


class BookQAService:
    """The textbook Q&A use case, scoped to a project."""

    def __init__(self, repo: Repository, router: ModelRouter, *, worker: IngestionWorker | None = None) -> None:
        self.repo = repo
        self.router = router
        self.worker = worker
        self.context_builder = ContextBuilder()

    def ingest(self, *, project_id: str, learner_id: str, book_id: str,
               source: bytes, filename: str, title: str = "") -> dict:
        """Upload + parse + chunk + index a textbook into the project's scope."""
        self.repo.assert_project_owned_by(project_id, learner_id)
        if self.worker is None:
            # ``ingest`` is retained only for the isolated compatibility API.
            # Product uploads use the persistent IngestionRunner/JobService.
            from ..jobs import JobStore, IngestionWorker
            self.worker = IngestionWorker(JobStore(), self.router)
        # Ensure the book record exists and is linked as PRIMARY if not already.
        from ..domain.enums import BookRole
        from ..domain.models import Book, ProjectBook
        if self.repo.get_source(book_id) is None:
            self.repo.add_book(Book(
                book_id=book_id, owner_user_id=learner_id,
                source_hash=_sha256(source), title=title or filename,
            ))
        try:
            self.repo.link_book(ProjectBook(project_id=project_id, book_id=book_id, role=BookRole.PRIMARY))
        except ScopeError:
            pass  # already linked
        # Run ingestion, indexing into the project's retriever (create if absent).
        ret = self.repo.get_retriever(project_id) or _new_retriever(self.router)
        res = self.worker.ingest(
            project_id=project_id, book_id=book_id, source=source,
            filename=filename, retriever=ret,
        )
        self.repo.set_retriever(project_id, res.retriever or ret)
        self.repo.add_chunks(book_id, res.chunks)
        return {
            "job_id": res.job.job_id, "state": res.job.state.value,
            "stage": res.job.stage.value, "chunks": len(res.chunks),
            "parser": res.job.parser, "reused": res.reused,
        }

    def ask(self, *, project_id: str, learner_id: str, question: str,
            top_k: int = 5, context_budget: int = 4,
            source_ids: list[str] | None = None,
            physical_page: int | None = None) -> AskResult:
        """Answer from the selected source range with grounded citations."""
        self.repo.assert_project_owned_by(project_id, learner_id)
        # P1-10: in ASSESSMENT mode the learner is being independently verified,
        # so exposing textbook excerpts + page citations would leak the answer
        # and invalidate the assessment. Refuse to retrieve/quote the book; the
        # orchestrator surfaces a short explanation instead. The mode is read
        # from the trusted project record, never the request body.
        from ..domain.enums import UIPreset
        mode = self.repo.get_project_mode(project_id)
        if mode == UIPreset.ASSESSMENT:
            concept_count = sum(
                len(self.repo.concepts_for_book(source_id))
                for source_id in self.repo.allowed_book_ids(project_id)
            )
            return AskResult(
                f"当前处于独立检测（评估模式）。这里只负责出题和记录独立作答，不会在这里讲解资料。当前范围包含 {concept_count} 个已整理知识点。"
                "要开始，请点击“开始一道检测题”或发送“开始检测”；如果已有题目，请直接作答或选择跳过。"
                "要查概念或原文，请回到“学习资料”。",
                [], False, [],
                "assessment mode: textbook lookup is disabled during an assessment",
            )
        ret = self.repo.get_retriever(project_id)
        if ret is None or len(ret.chunks) == 0:
            # M3: after a restart the in-memory retriever is gone. If chunks
            # were persisted to disk during ingestion, rebuild the retriever
            # from them so Q&A keeps working without re-uploading.
            self._maybe_rebuild_retriever(project_id, learner_id)
            ret = self.repo.get_retriever(project_id)
        if ret is None or len(ret.chunks) == 0:
            return AskResult("", [], False, [], "no processed learning source; add or ingest a source first")
        # Hard-filter to this project's allowed chunks.
        allowed_book_ids = self.repo.allowed_book_ids(project_id)
        if source_ids:
            allowed_book_ids &= set(source_ids)
        scoped_chunks = [
            chunk for chunk in self.repo.chunks_for_project(project_id)
            if chunk.book_id in allowed_book_ids
            and (physical_page is None or chunk.source_ref.physical_page == physical_page)
        ]
        allow_chunks = {c.chunk_id for c in scoped_chunks}
        if not allow_chunks:
            return AskResult("", [], False, [], "no content in the selected source range")
        hits = ret.retrieve(
            question, top_k=top_k, context_budget=context_budget,
            allow_chunk_ids=allow_chunks,
        )
        if not hits:
            return AskResult("", [], False, [], "no relevant chunks found")
        # Build context (mode-agnostic for a plain question → Reading/Proactive).
        ctx = self.context_builder.build(ContextRequest(
            activity_mode=ActivityMode.READING,
            intervention_policy=InterventionPolicy.PROACTIVE,
            retrieved_chunks=[h.chunk for h in hits],
        ))
        validator = CitationValidator(
            {c.chunk_id: c for c in scoped_chunks},
            allowed_book_ids,
        )
        tutor = TutorAgent(self.router, validator)
        # Product requests prefer one bounded, structured model call. A second
        # full provider/fallback chain made malformed citations feel like the
        # app had frozen; citation failure now returns an honest refusal.
        # Grounded textbook answers are intentionally concise. A 4096-token
        # output budget made reasoning models run until the transport timeout;
        # 1024 leaves ample answer space while keeping latency predictable.
        ans = tutor.answer(question, hits, max_attempts=1, max_tokens=1024)
        return AskResult(
            answer_text=ans.text, citations=ans.citations,
            grounded=ans.grounded, chunk_ids=ans.chunk_ids, reason=ans.reason,
        )

    def _maybe_rebuild_retriever(self, project_id: str, learner_id: str) -> None:
        """Best-effort post-restart recovery: if the in-memory retriever is
        gone but chunks were persisted to disk during ingestion (M3), rebuild
        it from those chunks. Old chunker caches are transparently regenerated
        from the parsed document so existing uploads gain indexing fixes."""
        import json
        from pathlib import Path
        from ..config import get_settings
        from ..retrieval.bm25 import BM25Index
        from ..retrieval.vector import VectorStore
        from ..retrieval.chunk import DocumentChunk
        from ..retrieval.chunking import CHUNKER_VERSION, Chunker, scope_chunks_to_book
        from ..retrieval.parsed_document import ParsedDocument
        settings = get_settings()
        book_ids = self.repo.allowed_book_ids(project_id)
        chunks: list[DocumentChunk] = []
        parsed_documents: dict[str, ParsedDocument] = {}
        for bid in book_ids:
            path = Path(settings.data_dir) / "indexes" / bid / "chunks.json"
            if path.is_file():
                try:
                    cached = [
                        DocumentChunk.model_validate(item)
                        for item in json.loads(path.read_text("utf-8"))
                    ]
                    rewrite_index = any(
                        chunk.chunker_version != CHUNKER_VERSION
                        or chunk.book_id != bid
                        or not chunk.chunk_id.startswith(f"{bid}:")
                        for chunk in cached
                    )
                    if cached and any(chunk.chunker_version != CHUNKER_VERSION for chunk in cached):
                        source = self.repo.get_source(bid)
                        parsed_candidates = []
                        if source is not None:
                            parsed_root = Path(settings.data_dir) / "parsed" / source.source_hash
                            if source.parser_version:
                                parsed_candidates.append(parsed_root / source.parser_version / "document.json")
                            parsed_candidates.extend(parsed_root.glob("*/document.json"))
                        parsed_path = next((candidate for candidate in parsed_candidates if candidate.is_file()), None)
                        if parsed_path is not None:
                            document = ParsedDocument.model_validate_json(parsed_path.read_text("utf-8"))
                            parsed_documents[bid] = document
                            cached = Chunker().chunk(document, bid)
                    cached = scope_chunks_to_book(cached, bid)
                    if rewrite_index:
                        path.write_text(
                            json.dumps(
                                [chunk.model_dump(mode="json") for chunk in cached],
                                ensure_ascii=False,
                            ),
                            encoding="utf-8",
                        )
                    chunks.extend(cached)
                except Exception:  # noqa: BLE001 — corrupt cache, skip
                    pass
        if not chunks:
            return
        ret = HybridRetriever(bm25_index=BM25Index(), vector_store=VectorStore(),
                              router=self.router, rerank_enabled=False)
        ret.index_chunks(chunks, use_local_embeddings=True)
        self.repo.set_retriever(project_id, ret)
        for bid in book_ids:
            self.repo.add_chunks(bid, [c for c in chunks if c.book_id == bid])
        # Old chunker-v1 graphs may point every section at ``...-chk-0``. Once
        # the index is repaired, rebuild only graphs whose references no longer
        # resolve to the same physical page. This keeps valid learner graphs
        # untouched and repairs legacy uploads transparently on first use.
        for bid in book_ids:
            book_chunks = [chunk for chunk in chunks if chunk.book_id == bid]
            if not self._graph_references_are_stale(bid, book_chunks):
                continue
            document = parsed_documents.get(bid) or self._load_parsed_document(bid)
            if document is None:
                continue
            from ..llm.router import ModelRouter, RouterConfig
            from .book_mapping import BookMappingService
            BookMappingService(
                self.repo, ModelRouter(RouterConfig(live=False)),
            ).map_book(
                project_id=project_id,
                learner_id=learner_id,
                book_id=bid,
                parsed_document=document,
                chunks=book_chunks,
                graph_key=f"repair:{document.parser_version}:{CHUNKER_VERSION}",
            )

    def _load_parsed_document(self, book_id: str):
        from pathlib import Path
        from ..config import get_settings
        from ..retrieval.parsed_document import ParsedDocument
        source = self.repo.get_source(book_id)
        if source is None:
            return None
        root = Path(get_settings().data_dir) / "parsed" / source.source_hash
        candidates = []
        if source.parser_version:
            candidates.append(root / source.parser_version / "document.json")
        candidates.extend(root.glob("*/document.json"))
        path = next((item for item in candidates if item.is_file()), None)
        if path is None:
            return None
        try:
            return ParsedDocument.model_validate_json(path.read_text("utf-8"))
        except Exception:  # noqa: BLE001 - corrupt cache remains recoverable
            return None

    def _graph_references_are_stale(
        self, book_id: str, chunks: list["DocumentChunk"],
    ) -> bool:
        concepts = self.repo.concepts_for_book(book_id)
        if not concepts:
            return True
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        checked = 0
        for concept in concepts:
            for ref in concept.source_refs:
                if not ref.chunk_id:
                    continue
                checked += 1
                chunk = by_id.get(ref.chunk_id)
                if chunk is None or chunk.source_ref.physical_page != ref.physical_page:
                    return True
        return checked == 0


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _new_retriever(router: ModelRouter) -> HybridRetriever:
    from ..retrieval.bm25 import BM25Index
    from ..retrieval.vector import VectorStore
    return HybridRetriever(bm25_index=BM25Index(), vector_store=VectorStore(),
                           router=router, rerank_enabled=False)
