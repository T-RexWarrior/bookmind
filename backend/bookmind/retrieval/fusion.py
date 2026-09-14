"""Hybrid retrieval — RRF fusion + optional reranker (ARCHITECTURE §5, §4.2).

Pipeline::

    question + current_section
    → Dense top-k  +  BM25 top-k
    → RRF fusion
    → optional reranker
    → project/book allowlist hard-filter
    → Context Budget

RRF (Reciprocal Rank Fusion) is rank-based, so it works even when BM25 scores
and dense similarities are on different scales. The reranker is only applied
when a live reranker is available and shown to help; otherwise the RRF order
is used directly (ARCHITECTURE §5: "Reranker 是增强项，不是核心依赖").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..llm.router import ModelRouter
from ..llm.schemas import RerankResult
from .chunk import DocumentChunk


@dataclass
class RetrievalHit:
    chunk: DocumentChunk
    bm25_rank: int | None = None  # 1-indexed; None if not in BM25 results
    dense_rank: int | None = None
    rrf_score: float = 0.0
    final_score: float = 0.0
    rerank_score: float | None = None


def reciprocal_rank_fusion(
    bm25_results: list[tuple[str, float]],
    dense_results: list[tuple[str, float]],
    *,
    k: int = 60,
) -> dict[str, float]:
    """Fuse two ranked lists into an RRF score map.

    ``k`` is the RRF constant (standard 60). Returns ``{chunk_id: rrf_score}``.
    """
    scores: dict[str, float] = {}
    for rank, (cid, _) in enumerate(bm25_results, start=1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    for rank, (cid, _) in enumerate(dense_results, start=1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores


@dataclass
class HybridRetriever:
    """Combines a BM25 index and a dense VectorStore with RRF.

    The retriever holds no network state of its own; it accepts a
    :class:`ModelRouter` for embeddings and (optional) reranking so the whole
    retrieval layer is deterministic under an offline router.
    """

    bm25_index: "BM25Index"  # noqa: F821 — forward ref to bm25.index
    vector_store: "VectorStore"
    router: ModelRouter
    chunks: dict[str, DocumentChunk] = field(default_factory=dict)
    rerank_enabled: bool = True

    def index_chunks(
        self,
        chunks: list[DocumentChunk],
        *,
        use_local_embeddings: bool = False,
    ) -> None:
        """Register chunks and build both indexes."""
        # Embed in one batch for efficiency; fall back per-chunk on failure.
        texts = [c.content for c in chunks]
        if not texts:
            return
        emb = self.router.embed_local(texts) if use_local_embeddings else self.router.embed(texts)
        for c, vec in zip(chunks, emb.vectors):
            c_with_space = c.model_copy(update={"embedding_space": emb.model})
            self.chunks[c.chunk_id] = c_with_space
            self.bm25_index.add(c_with_space)
            self.vector_store.add(c_with_space, vec)

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        allow_chunk_ids: set[str] | None = None,
        current_section_path: tuple[str, ...] | None = None,
        context_budget: int = 4,
    ) -> list[RetrievalHit]:
        """Run hybrid retrieval and return the top hits within budget.

        ``allow_chunk_ids`` enforces the project/book allowlist hard-filter
        (ARCHITECTURE §7: "所有检索先应用 active_project_id + allowed_book_ids 硬过滤").
        """
        # 1. Lexical recall first. Most textbook questions contain an exact
        # concept or operation name. In that common case BM25 is both more
        # precise and avoids a network embedding call on every message. Dense
        # retrieval remains the fallback for paraphrases with no lexical hit.
        bm25_hits = self.bm25_index.search(
            query,
            k=max(top_k * 3, 10),
            allow_chunk_ids=allow_chunk_ids,
        )
        # Mixed Chinese/technical queries often contain the discriminating
        # identifier in ASCII (for example ``bitmap`` or ``equals``). BM25 can
        # otherwise rank unrelated paragraphs sharing generic Chinese phrases
        # such as “改进版” above the actual concept. If at least one result
        # contains every ASCII identifier, discard candidates that contain
        # none of them.
        ascii_terms = re.findall(r"[a-z][a-z0-9_+#]*", query.lower())
        if ascii_terms:
            specific_hits = [
                hit for hit in bm25_hits
                if (chunk := self.chunks.get(hit[0])) is not None
                and all(term in chunk.content.lower() for term in ascii_terms)
            ]
            if specific_hits:
                bm25_hits = specific_hits
        dense_hits: list[tuple[str, float]] = []
        if not bm25_hits:
            qvec_res = (
                self.router.embed_local([query])
                if self.vector_store.embedding_space == "offline-hash-256"
                else self.router.embed([query])
            )
            qvec = qvec_res.vectors[0] if qvec_res.vectors else []
            dense_hits = self.vector_store.search(
                qvec,
                k=max(top_k * 3, 10),
                allow_chunk_ids=allow_chunk_ids,
            ) if qvec else []

        # 2. RRF fusion.
        rrf = reciprocal_rank_fusion(bm25_hits, dense_hits)

        # 3. Allowlist hard-filter.
        candidate_ids = [cid for cid in rrf if (allow_chunk_ids is None or cid in allow_chunk_ids)]
        # Rank by RRF score desc, then chunk_id for determinism.
        candidate_ids.sort(key=lambda cid: (-rrf[cid], cid))

        hits: list[RetrievalHit] = []
        bm25_rank_map = {cid: r for r, (cid, _) in enumerate(bm25_hits, start=1)}
        dense_rank_map = {cid: r for r, (cid, _) in enumerate(dense_hits, start=1)}
        for cid in candidate_ids:
            chunk = self.chunks.get(cid)
            if chunk is None:
                continue
            hits.append(RetrievalHit(
                chunk=chunk,
                bm25_rank=bm25_rank_map.get(cid),
                dense_rank=dense_rank_map.get(cid),
                rrf_score=rrf[cid],
            ))
            if len(hits) >= max(top_k, context_budget) * 2:
                break

        # 4. Optional reranker over the shortlist.
        # A lexical hit is already ordered deterministically and should return
        # immediately. Only pay for remote reranking on the semantic fallback
        # path; otherwise a simple exact-term question can wait on two network
        # calls before the answer model even starts.
        if self.rerank_enabled and dense_hits and len(hits) > 1:
            short = hits[: max(top_k, context_budget) * 2]
            rr = self.router.rerank(query, [h.chunk.content for h in short])
            if rr.ok and len(rr.scores) == len(short):
                for h, s in zip(short, rr.scores):
                    h.rerank_score = s
                short.sort(key=lambda h: (-(h.rerank_score or 0.0), h.chunk.chunk_id))
                for h in short:
                    h.final_score = h.rerank_score or h.rrf_score
                hits = short

        # 5. Context budget — final trim.
        hits = hits[:context_budget]
        if not hits:
            return []
        # If no rerank happened, final_score stays at rrf_score.
        for h in hits:
            if h.final_score == 0.0:
                h.final_score = h.rrf_score
        return hits
