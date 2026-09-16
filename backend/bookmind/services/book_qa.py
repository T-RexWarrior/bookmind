"""Grounded source QA service — orchestrates retrieval and cited answers.

Wires retrieval → context → Tutor → citation validation, all under the
project/book scope. This is the service the HTTP API calls; it owns the
transactions and scope checks that Agents and the Engine must not bypass.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

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
    retrieval_confidence: float = 0.0
    fallback: bool = False


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
            top_k: int = 12, context_budget: int = 12,
            source_ids: list[str] | None = None,
            physical_page: int | None = None,
            on_retrieval: Callable[[list[dict], float, bool], None] | None = None) -> AskResult:
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
            and (
                physical_page is None
                or (chunk.page_start or chunk.source_ref.physical_page) <= physical_page
                <= (chunk.page_end or chunk.page_start or chunk.source_ref.physical_page)
            )
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
        if physical_page is not None:
            for hit in hits:
                if hit.bm25_rank is not None:
                    hit.confidence = max(hit.confidence, 0.85)
                    hit.confidence_label = "HIGH"
        reliable_hits = [hit for hit in hits if hit.confidence_label == "HIGH"]
        preliminary_locations = _server_locations(reliable_hits)
        retrieval_confidence = max((hit.confidence for hit in hits), default=0.0)
        if on_retrieval:
            on_retrieval(preliminary_locations, retrieval_confidence, bool(reliable_hits))
        if not reliable_hits:
            return AskResult(
                answer_text=(
                    "未能在当前教材范围中可靠定位足够依据，本次不生成可能失真的回答。\n\n"
                    "你可以切换到具体页面、选中一段原文后再问，或换用教材中的术语描述问题。"
                ),
                citations=[], grounded=False, chunk_ids=[],
                reason="no high-confidence retrieval evidence",
                retrieval_confidence=retrieval_confidence,
            )
        # A high-confidence hit opens the evidence gate. Its lower-scored
        # same-section neighbours remain useful context (definitions and lists
        # are commonly split across chunk boundaries), but the Tutor must cite
        # the exact supporting chunk before any of them can appear in an answer.
        answer_hits = hits
        # Build context (mode-agnostic for a plain question → Reading/Proactive).
        ctx = self.context_builder.build(ContextRequest(
            activity_mode=ActivityMode.READING,
            intervention_policy=InterventionPolicy.PROACTIVE,
            retrieved_chunks=[h.chunk for h in answer_hits],
        ))
        validator = CitationValidator(
            {c.chunk_id: c for c in scoped_chunks},
            allowed_book_ids,
        )
        tutor = TutorAgent(self.router, validator)
        # deepseek-flash can use a substantial part of the completion budget
        # for reasoning before emitting the short JSON answer.  A 4096-token
        # ceiling prevents false empty-content fallbacks; the prompt still asks
        # for a concise learner-facing response.
        ans = tutor.answer(
            question, answer_hits, max_attempts=2, max_tokens=4096,
        )
        supported_ids = set(ans.chunk_ids) if ans.grounded else set()
        supported_hits = [
            hit for hit in answer_hits if hit.chunk.chunk_id in supported_ids
        ]
        quote_by_id = {
            str(item.get("chunk_id") or ""): str(item.get("quote") or "")
            for item in ans.citations
        }
        citations = _server_locations(supported_hits)
        for citation in citations:
            citation["quote"] = quote_by_id.get(citation["chunk_id"], "")
        grounded = bool(ans.grounded and supported_hits)
        answer_text = ans.text
        reason = ans.reason
        if ans.fallback:
            if reliable_hits:
                answer_text = "模型暂时不可用，本次没有生成回答。\n\n你可以先打开相关原文阅读，稍后重新生成回答。"
                # Locations remain server-derived and safe even though no answer
                # claim passed the evidence gate.
                citations = preliminary_locations
            else:
                answer_text = (
                    "模型暂时不可用，本次没有生成回答。\n\n"
                    "当前也未能在教材中可靠定位相关内容，请选择具体页面或换一种问法。"
                )
        elif not grounded:
            answer_text = (
                f"{answer_text}\n\n回答的教材依据校验未通过，本次不记录知识点疑问。"
            )
        if not ans.fallback and answer_text:
            answer_text = f"[AI综合回答]\n\n{answer_text}\n\nAI回答可能不完全正确，请结合教材原文核对。"
        return AskResult(
            answer_text=answer_text, citations=citations,
            grounded=grounded, chunk_ids=[hit.chunk.chunk_id for hit in supported_hits],
            reason=reason,
            retrieval_confidence=retrieval_confidence,
            fallback=ans.fallback,
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
                                parsed_candidates.extend((parsed_root / source.parser_version).glob("*/document.json"))
                            parsed_candidates.extend(parsed_root.glob("*/*/document.json"))
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
                              router=self.router, rerank_enabled=True)
        # Restore the exact persisted embedding vectors. Never silently replace
        # a live embedding space with local hash vectors after restart.
        for chunk in chunks:
            ret.chunks[chunk.chunk_id] = chunk
            ret.bm25_index.add(chunk)
        from ..retrieval.persistent_index import load_vectors
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        active_space = ""
        for bid in book_ids:
            ids, matrix, model = load_vectors(Path(settings.data_dir) / "indexes" / bid)
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
            candidates.extend((root / source.parser_version).glob("*/document.json"))
        candidates.extend(root.glob("*/*/document.json"))
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


def _server_locations(hits: list[RetrievalHit]) -> list[dict]:
    locations: list[dict] = []
    seen: set[tuple] = set()
    for hit in hits:
        chunk = hit.chunk
        start = chunk.page_start or chunk.source_ref.physical_page
        end = chunk.page_end or start
        key = (chunk.book_id, chunk.section_path, start, end)
        if key in seen:
            continue
        seen.add(key)
        locations.append({
            "chunk_id": chunk.chunk_id, "book_id": chunk.book_id,
            "section_path": list(chunk.section_path), "page": str(start),
            "page_start": start, "page_end": end,
            "confidence": hit.confidence,
        })
        if len(locations) >= 3:
            break
    return locations


def _new_retriever(router: ModelRouter) -> HybridRetriever:
    from ..retrieval.bm25 import BM25Index
    from ..retrieval.vector import VectorStore
    return HybridRetriever(bm25_index=BM25Index(), vector_store=VectorStore(),
                           router=router, rerank_enabled=False)
