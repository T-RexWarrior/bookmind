"""Hybrid retrieval — RRF fusion + optional reranker (ARCHITECTURE §5, §4.2).

Pipeline::

    question + current_section
    → BM25 top-k (+ DeepSeek cross-language query expansion when needed)
      + optional compatible Dense top-k
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

from ..llm.router import ModelRouter, _tokenize
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
    bm25_score: float | None = None
    dense_score: float | None = None
    confidence: float = 0.0
    confidence_label: str = "LOW"
    adjacent_context: bool = False


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


def _fuse_lexical_queries(
    result_lists: list[list[tuple[str, float]]], *, k: int = 60,
) -> list[tuple[str, float]]:
    """Fuse independent query-facet result lists into one lexical ranking."""
    scores: dict[str, float] = {}
    for results in result_lists:
        for rank, (chunk_id, _) in enumerate(results, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


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
        allow_embedding_fallback: bool = True,
    ):
        """Register chunks and build both indexes."""
        # Embed in one batch for efficiency; fall back per-chunk on failure.
        texts = [c.content for c in chunks]
        if not texts:
            return None
        # The keyword index is independently publishable: an embedding outage
        # must not make a complete textbook unsearchable.
        for c in chunks:
            self.chunks[c.chunk_id] = c
            self.bm25_index.add(c)
        batches = [texts[index:index + 64] for index in range(0, len(texts), 64)]
        results = [
            self.router.embed_local(batch) if use_local_embeddings else self.router.embed(batch)
            for batch in batches
        ]
        failed = next((result for result in results if result.fallback or not result.ok), None)
        if failed is not None and not allow_embedding_fallback:
            return failed
        emb = results[0]
        vectors = [vector for result in results for vector in result.vectors]
        if any(result.model != emb.model or result.dim != emb.dim for result in results):
            return emb.model_copy(update={"fallback": True, "vectors": []})
        for c, vec in zip(chunks, vectors):
            c_with_space = c.model_copy(update={"embedding_space": emb.model})
            self.chunks[c.chunk_id] = c_with_space
            self.vector_store.add(c_with_space, vec)
        return emb

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
        # Navigation back matter contains dense lists of every keyword and can
        # outrank the actual explanation for nearly any question. Keep it out
        # of ordinary QA recall; the PDF outline remains separately available
        # to the reader UI.
        base_allow = allow_chunk_ids or set(self.chunks)
        content_allow = {
            chunk_id for chunk_id in base_allow
            if chunk_id in self.chunks and not _navigation_only(self.chunks[chunk_id])
        }
        search_allow = content_allow or base_allow
        # 1. Run lexical recall first. In the all-DeepSeek product setup there
        # is intentionally no remote dense model, so DeepSeek supplies compact
        # query facets and cross-language terms before deterministic recall.
        # A single Latin letter is commonly only a label inside a translated
        # Chinese term (B-树, d叉堆, 大O). Requiring that letter in every
        # passage incorrectly discards continuation paragraphs which omit the
        # heading. Multi-character identifiers such as KMP/AVL/Bitmap remain
        # useful hard constraints.
        explicit_ascii_terms = [
            term for term in re.findall(r"[a-z][a-z0-9_+#]*", query.lower())
            if len(term) >= 2
        ]
        contains_cjk = bool(re.search(r"[\u3400-\u9fff]", query))
        # Each generated phrase represents a separate facet. Searching them
        # independently and fusing ranks prevents a broad multi-part question
        # from retrieving only its highest-frequency subtopic.
        expanded: list[str] = self.router.expand_query(query) if contains_cjk else []
        # Keep the learner's complete question as one search alongside the
        # independently generated facets.  The former favours a passage that
        # answers several clauses together; the latter prevents one frequent
        # clause from drowning out the others.
        literal_clauses = [
            clause.strip()
            for clause in re.split(r"[？?；;，,。]+", query)
            if len(clause.strip()) >= 3
        ][:4]
        keyword_capsule = _keyword_capsule(query)
        clause_capsules = [_keyword_capsule(clause) for clause in literal_clauses]
        subject = clause_capsules[0].split()[0] if clause_capsules else ""
        contextual_clauses = [
            f"{subject} {clause}".strip()
            for clause in clause_capsules[1:]
        ]
        structure_probe = ""
        if "结构" in query and any(marker in query for marker in ("什么", "哪些", "依赖", "使用")):
            operations = " ".join(
                marker for marker in (
                    "遍历", "查找", "搜索", "排序", "匹配", "编码", "解析", "调度",
                )
                if marker in query
            )
            # Stack and queue are the two common implicit auxiliary structures
            # in textbook algorithm questions. A short probe is intentional:
            # appending every structure name or the whole question makes the
            # known traversal names dominate the missing implementation clue.
            structure_probe = f"{subject} {operations} stack queue".strip()
        # A passage containing several answer-bearing terms should outrank
        # chapter introductions that happen to repeat only the question. Keep
        # facet searches, but also search their combined vocabulary once.
        expanded_bundle = " ".join(expanded)
        lexical_queries = list(dict.fromkeys([
            keyword_capsule, expanded_bundle, structure_probe, query,
            *contextual_clauses, *literal_clauses, *expanded,
        ]))
        lexical_queries = [phrase for phrase in lexical_queries if phrase.strip()]
        lexical_lists = [
            self.bm25_index.search(
                phrase, k=max(top_k * 2, 8), allow_chunk_ids=search_allow,
            )
            for phrase in lexical_queries
        ]
        fused_lexical = _fuse_lexical_queries(lexical_lists)
        fused_score_map = dict(fused_lexical)
        # Reserve the best literal hit for every query facet.  These protected
        # candidates are carried through shortlist reranking so a multi-part
        # textbook question cannot silently lose one of its subquestions.
        facet_anchor_candidates: list[str] = []
        for phrase, results in zip(lexical_queries, lexical_lists):
            # A “which data structure” probe deliberately contains several
            # possible structure names. Keep its first two hits so the first
            # incidental match cannot hide the canonical implementation.
            keep = 2 if structure_probe and phrase == structure_probe else 1
            facet_anchor_candidates.extend(cid for cid, _ in results[:keep])
        facet_anchor_ids = list(dict.fromkeys(
            facet_anchor_candidates
        ))[:max(1, context_budget - 4)]
        bm25_hits = [
            (cid, fused_score_map[cid]) for cid in facet_anchor_ids
        ] + [
            item for item in fused_lexical if item[0] not in facet_anchor_ids
        ]
        if not bm25_hits:
            expanded = expanded or self.router.expand_query(query)
            if expanded:
                bm25_hits = _fuse_lexical_queries([
                    self.bm25_index.search(
                        phrase, k=max(top_k * 2, 8),
                        allow_chunk_ids=search_allow,
                    )
                    for phrase in expanded
                ])
        bm25_hits = bm25_hits[:max(top_k * 3, 10)]
        dense_hits: list[tuple[str, float]] = []
        if len(self.vector_store):
            qvec_res = (
                self.router.embed_local([query])
                if self.vector_store.embedding_space == "offline-hash-256"
                else self.router.embed([query])
            )
            qvec = qvec_res.vectors[0] if qvec_res.vectors else []
            query_space_matches = (
                qvec_res.ok
                and not qvec_res.fallback
                and qvec_res.model == self.vector_store.embedding_space
                and len(qvec) == self.vector_store.dimension
            ) or (
                self.vector_store.embedding_space == "offline-hash-256"
                and qvec_res.model == "offline-hash-256"
                and len(qvec) == self.vector_store.dimension
            )
            dense_hits = self.vector_store.search(
                qvec, k=max(top_k * 3, 10),
                allow_chunk_ids=search_allow,
            ) if qvec and query_space_matches else []
        # Only terms explicitly typed by the learner are hard constraints.
        # Expansion terms are recall hints and must never over-filter results.
        ascii_terms = explicit_ascii_terms
        if ascii_terms:
            matching_ids = {
                cid for cid, chunk in self.chunks.items()
                if all(term in chunk.content.lower() for term in ascii_terms)
            }
            if matching_ids:
                bm25_hits = [hit for hit in bm25_hits if hit[0] in matching_ids]
                dense_hits = [hit for hit in dense_hits if hit[0] in matching_ids]

        # 2. RRF fusion.
        rrf = reciprocal_rank_fusion(bm25_hits, dense_hits)

        # 3. Allowlist hard-filter.
        candidate_ids = [cid for cid in rrf if cid in search_allow]
        # Rank by RRF score desc, then chunk_id for determinism.
        candidate_ids.sort(key=lambda cid: (-rrf[cid], cid))

        hits: list[RetrievalHit] = []
        bm25_rank_map = {cid: r for r, (cid, _) in enumerate(bm25_hits, start=1)}
        dense_rank_map = {cid: r for r, (cid, _) in enumerate(dense_hits, start=1)}
        bm25_score_map = dict(bm25_hits)
        dense_score_map = dict(dense_hits)
        for cid in candidate_ids:
            chunk = self.chunks.get(cid)
            if chunk is None:
                continue
            hits.append(RetrievalHit(
                chunk=chunk,
                bm25_rank=bm25_rank_map.get(cid),
                dense_rank=dense_rank_map.get(cid),
                rrf_score=rrf[cid],
                bm25_score=bm25_score_map.get(cid),
                dense_score=dense_score_map.get(cid),
            ))
            if len(hits) >= max(top_k, context_budget) * 2:
                break

        # A formula, proof, or complexity conclusion often starts in the next
        # chunk. Put both neighbours of each facet anchor into the semantic
        # shortlist, rather than reserving an arbitrary pair only after
        # reranking. This is especially important for multi-part questions.
        if hits:
            ordered_ids = list(self.chunks)
            positions = {cid: index for index, cid in enumerate(ordered_ids)}
            hit_by_id = {h.chunk.chunk_id: h for h in hits}
            neighbour_hits: dict[str, RetrievalHit] = {}

            def add_neighbours(hit: RetrievalHit) -> list[RetrievalHit]:
                found: list[RetrievalHit] = []
                position = positions.get(hit.chunk.chunk_id)
                if position is None:
                    return found
                for neighbour_index in (position - 1, position + 1):
                    if neighbour_index < 0 or neighbour_index >= len(ordered_ids):
                        continue
                    neighbour_id = ordered_ids[neighbour_index]
                    if neighbour_id not in search_allow:
                        continue
                    neighbour = self.chunks[neighbour_id]
                    if (
                        not hit.chunk.parent_chunk_id
                        or neighbour.parent_chunk_id != hit.chunk.parent_chunk_id
                    ):
                        continue
                    neighbour_hit = neighbour_hits.setdefault(
                        neighbour_id,
                        RetrievalHit(
                            chunk=neighbour,
                            rrf_score=hit.rrf_score * 0.95,
                            final_score=hit.rrf_score * 0.95,
                            adjacent_context=True,
                        ),
                    )
                    found.append(neighbour_hit)
                return found

            augmented: list[RetrievalHit] = []
            augmented_ids: set[str] = set()

            def append_once(hit: RetrievalHit) -> None:
                if hit.chunk.chunk_id not in augmented_ids:
                    augmented.append(hit)
                    augmented_ids.add(hit.chunk.chunk_id)

            # Anchors and their local windows go first, followed by all other
            # lexical candidates. DeepSeek then decides which windows really
            # support the complete question.
            for anchor_id in facet_anchor_ids:
                anchor = hit_by_id.get(anchor_id)
                if anchor is None:
                    continue
                append_once(anchor)
                for neighbour_hit in add_neighbours(anchor):
                    append_once(neighbour_hit)
            for hit in hits:
                append_once(hit)
                for neighbour_hit in add_neighbours(hit):
                    append_once(neighbour_hit)
            hits = augmented

        # 4. Optional reranker over the shortlist.
        # DeepSeek product mode intentionally uses this step to rerank the small
        # lexical shortlist because its official API has no embeddings endpoint.
        if self.rerank_enabled and len(hits) > 1:
            shortlist_size = max(12, min(24, context_budget * 2))
            short = hits[:shortlist_size]
            rr = self.router.rerank(query, [h.chunk.content for h in short])
            if rr.ok and len(rr.scores) == len(short):
                for h, s in zip(short, rr.scores):
                    h.rerank_score = s
                short.sort(key=lambda h: (-(h.rerank_score or 0.0), h.chunk.chunk_id))
                for h in short:
                    h.final_score = h.rerank_score or h.rrf_score
                # Preserve one best literal result per planned facet, then
                # fill remaining slots by semantic relevance.  DeepSeek's
                # scores still order the final candidates, but cannot erase a
                # low-frequency clause such as “复杂度” or “发生上溢时”.
                by_id = {h.chunk.chunk_id: h for h in short}
                protected = [by_id[cid] for cid in facet_anchor_ids if cid in by_id]
                protected_ids = {h.chunk.chunk_id for h in protected}
                hits = protected + [
                    h for h in short if h.chunk.chunk_id not in protected_ids
                ]

        # 5. Context budget — final trim.
        hits = hits[:context_budget]
        if not hits:
            return []
        # If no rerank happened, final_score stays at rrf_score.
        for h in hits:
            if h.final_score == 0.0:
                h.final_score = h.rrf_score
            both = h.bm25_rank is not None and h.dense_rank is not None
            exact_ascii = bool(ascii_terms) and all(
                term in h.chunk.content.lower() for term in ascii_terms
            )
            distinctive_ascii = any(
                len(term) >= 2 and term in (
                    h.chunk.content + " " + " ".join(h.chunk.section_path)
                ).lower()
                for term in ascii_terms
            )
            query_terms = set(_tokenize(query))
            lexical_coverage = (
                len(query_terms & set(_tokenize(h.chunk.content))) / len(query_terms)
                if query_terms else 0.0
            )
            if both:
                h.confidence = 0.90
            elif h.rerank_score is not None and h.rerank_score >= 0.65:
                h.confidence = min(0.95, h.rerank_score)
            elif exact_ascii and h.bm25_rank is not None:
                h.confidence = 0.82
            elif distinctive_ascii and h.bm25_rank == 1:
                h.confidence = 0.82
            elif expanded and h.bm25_rank == 1 and (h.bm25_score or 0) > 0:
                h.confidence = 0.82
            elif h.bm25_rank == 1 and lexical_coverage >= 0.45:
                h.confidence = 0.82
            elif h.bm25_rank == 1 and (h.bm25_score or 0) > 0:
                h.confidence = 0.62
            else:
                h.confidence = 0.35
            h.confidence_label = "HIGH" if h.confidence >= 0.80 else "LOW"
        return hits


def _navigation_only(chunk: DocumentChunk) -> bool:
    path = " ".join(chunk.section_path).casefold()
    markers = (
        "目录", "关键词索引", "插图索引", "表格索引", "代码索引", "算法索引",
        "参考文献", "致谢", "教学计划编排", "第1版前言", "第2版说明", "第3版说明",
    )
    return any(marker.casefold() in path for marker in markers)


def _keyword_capsule(query: str) -> str:
    """Remove question glue while preserving literal technical terms.

    The shared CJK tokenizer emits bigrams/trigrams.  Keeping a long natural
    question intact therefore creates many high-frequency bigrams such as
    “为什么” and “是什么”.  Replacing only grammatical glue with boundaries
    yields a deterministic high-signal query such as
    ``d叉堆 高度 上滤 下滤复杂度 乘以d``.
    """
    value = query
    glue = (
        "教材中", "教材给出的", "教材", "请结合", "请说明", "请解释",
        "为什么", "是什么", "有什么", "有何", "分别", "如何", "怎样",
        "怎么", "还要", "可以", "能够", "通过", "进行", "给出", "说明",
        "其中", "以及", "并且", "及其", "是否", "一个", "一种", "这个",
        "那个", "哪些", "什么", "时候", "的", "了", "和", "与", "及",
    )
    for phrase in glue:
        value = value.replace(phrase, " ")
    value = re.sub(r"[^0-9A-Za-z_+#\-\u3400-\u9fff]+", " ", value)
    return " ".join(value.split()) or query
