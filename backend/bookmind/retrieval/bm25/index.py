"""BM25 index — pure-Python lexical retrieval (ARCHITECTURE §5).

Part of the Hybrid RAG pipeline: BM25 top-k runs in parallel with dense
top-k, then RRF fuses them. The index uses the same tokeniser as the offline
embedding fallback and the chunker so term matching is consistent across the
system.

No third-party dependency; this is deliberately small and explainable. For a
500-page textbook the in-memory index is still fast enough for the demo, and a
production deployment can swap a Postgres FTS index behind the same interface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ...llm.router import _tokenize
from ..chunk import DocumentChunk


@dataclass
class _DocEntry:
    chunk_id: str
    tokens: list[str]
    tf: dict[str, int] = field(default_factory=dict)
    length: int = 0


class BM25Index:
    """Okapi BM25 over a fixed corpus of chunks."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: dict[str, _DocEntry] = {}
        self._df: dict[str, int] = {}  # document frequency per term
        self._total_length = 0
        self._n = 0
        self._avgdl: float = 0.0

    def add(self, chunk: DocumentChunk) -> None:
        if chunk.chunk_id in self._docs:
            return  # idempotent add
        toks = _tokenize(chunk.content)
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        entry = _DocEntry(chunk_id=chunk.chunk_id, tokens=toks, tf=tf, length=len(toks))
        self._docs[chunk.chunk_id] = entry
        self._total_length += entry.length
        self._n += 1
        self._avgdl = self._total_length / self._n if self._n else 0.0
        for term in tf:
            self._df[term] = self._df.get(term, 0) + 1

    def add_many(self, chunks: list[DocumentChunk]) -> None:
        for c in chunks:
            self.add(c)

    def __len__(self) -> int:
        return self._n

    def search(
        self, query: str, k: int = 10, *, allow_chunk_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Return the best pairs, ranking only inside the allowed scope."""
        # Query expansion can repeat a concept across several phrases. Treat
        # the query as a set of retrieval clues so generic repeated words do
        # not swamp a more specific term such as 输入、输出 or 平衡因子.
        q_terms = list(dict.fromkeys(_tokenize(query)))
        if not q_terms or self._n == 0:
            return []
        scores: dict[str, float] = {}
        for term in q_terms:
            df = self._df.get(term, 0)
            if df == 0:
                continue
            idf = math.log(1 + (self._n - df + 0.5) / (df + 0.5))
            for cid, entry in self._docs.items():
                if allow_chunk_ids is not None and cid not in allow_chunk_ids:
                    continue
                f = entry.tf.get(term, 0)
                if f == 0:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * (entry.length / (self._avgdl or 1.0)))
                scores[cid] = scores.get(cid, 0.0) + idf * (f * (self.k1 + 1)) / denom
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:k]
