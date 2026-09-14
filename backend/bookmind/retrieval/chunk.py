"""DocumentChunk — the leaf retrieval unit (ARCHITECTURE.md §5).

A chunk is sliced from blocks within a single section, preserving the title
path, adjacency and source location so any answer can be traced back to a
physical page and block. Chunks are what BM25 and the dense index store.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..domain.source_ref import SourceRef


class DocumentChunk(BaseModel):
    chunk_id: str
    book_id: str
    document_id: str
    section_id: str | None = None
    section_path: tuple[str, ...] = ()
    content: str
    source_ref: SourceRef
    # The block_ids this chunk was assembled from (provenance for citation).
    block_ids: list[str] = Field(default_factory=list)
    # Char offset of this chunk's content within the concatenated section text.
    char_range: tuple[int, int] | None = None
    parser_version: str = ""
    embedding_space: str = ""  # which embedding model/dim produced the vector
    chunker_version: str = "chunker_v1"

    model_config = {"frozen": True}

    def short_label(self) -> str:
        """A compact citation label, e.g. 'p.42 · 3.2 Polymorphism'."""
        page = self.source_ref.printed_page or str(self.source_ref.physical_page)
        path = " · ".join(self.section_path) if self.section_path else ""
        return f"p.{page} · {path}" if path else f"p.{page}"
