"""Dense vector store — pure-Python in-memory cosine index (ARCHITECTURE §5).

The offline/demo path stores vectors in memory and ranks by cosine similarity.
A production deployment swaps a pgvector-backed store behind the same interface.
The store is keyed by chunk_id and is embedding-space aware: changing the
embedding model/dimension requires rebuilding the index (ARCHITECTURE §8
"embedding model/dimension 变化必须重建对应索引").
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..chunk import DocumentChunk


@dataclass
class _VectorEntry:
    chunk_id: str
    vector: list[float]
    norm: float


class VectorStore:
    """In-memory cosine-similarity index over chunk embeddings."""

    def __init__(self) -> None:
        self._entries: dict[str, _VectorEntry] = {}
        self._vectors: list[_VectorEntry] = []  # for iteration
        self._embedding_space: str = ""

    @property
    def embedding_space(self) -> str:
        return self._embedding_space

    def add(self, chunk: DocumentChunk, vector: list[float]) -> None:
        # All chunks in one index must share one embedding space. The first
        # non-empty space wins; any later chunk with a different non-empty
        # space is rejected (ARCHITECTURE §8: changing embedding model/dim
        # requires rebuilding the index).
        cspace = chunk.embedding_space
        if cspace:
            if not self._embedding_space:
                self._embedding_space = cspace
            elif self._embedding_space and cspace != self._embedding_space:
                raise ValueError(
                    f"embedding space mismatch: index={self._embedding_space} chunk={cspace}; "
                    "rebuild the index when the embedding model/dimension changes"
                )
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        entry = _VectorEntry(chunk.chunk_id, vector, norm)
        if chunk.chunk_id in self._entries:
            # replace on re-index
            self._vectors = [e for e in self._vectors if e.chunk_id != chunk.chunk_id]
        self._entries[chunk.chunk_id] = entry
        self._vectors.append(entry)

    def add_many(self, pairs: list[tuple[DocumentChunk, list[float]]]) -> None:
        for chunk, vec in pairs:
            self.add(chunk, vec)

    def __len__(self) -> int:
        return len(self._entries)

    def search(
        self, query_vector: list[float], k: int = 10,
        *, allow_chunk_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Return the best pairs, ranking only inside the allowed scope."""
        if not self._vectors:
            return []
        qn = math.sqrt(sum(v * v for v in query_vector)) or 1.0
        scored: list[tuple[str, float]] = []
        for e in self._vectors:
            if allow_chunk_ids is not None and e.chunk_id not in allow_chunk_ids:
                continue
            dot = sum(a * b for a, b in zip(query_vector, e.vector))
            sim = dot / (qn * e.norm)
            scored.append((e.chunk_id, sim))
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return scored[:k]

    def vector_for(self, chunk_id: str) -> list[float] | None:
        e = self._entries.get(chunk_id)
        return list(e.vector) if e else None
