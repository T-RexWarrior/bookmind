"""ParsedDocument — the unified parser output (OPEN_SOURCE_REFERENCES.md §6).

Every parser (MinerU, PyPdf, RapidOCR, PlainPdfFallback) emits this shape; nothing
downstream depends on parser-private fields. A ParsedDocument preserves the
four-layer representation from ARCHITECTURE.md §4.1.1:

    L0 raw    → raw_artifact_path
    L1 struct → pages / sections / blocks (reading order, page, bbox)
    L2 chunk  → produced by ``chunking`` from blocks
    L3 sema   → produced by the Book Mapper from sections + chunks
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..domain.source_ref import SourceRef


class Block(BaseModel):
    """A leaf layout block — the unit the parser identifies on a page."""

    block_id: str
    block_type: str = "text"  # text | heading | code | table | list | image
    text: str = ""
    physical_page: int
    printed_page: str | None = None
    bbox: tuple[float, float, float, float] | None = None  # x0,y0,x1,y1
    reading_order: int = 0
    # The section path this block belongs to, e.g. ("Chapter 3", "3.2 Polymorphism").
    section_path: tuple[str, ...] = ()

    model_config = {"frozen": True}


class Section(BaseModel):
    """A titled section within the document."""

    section_id: str
    title: str
    section_path: tuple[str, ...] = ()  # full path including ancestors
    physical_page: int
    printed_page: str | None = None
    block_ids: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}


class Page(BaseModel):
    """A physical page of the source PDF."""

    physical_page: int
    printed_page: str | None = None
    width: float | None = None
    height: float | None = None
    block_ids: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}


class ParsedDocument(BaseModel):
    """The unified, parser-agnostic representation of one textbook."""

    document_id: str
    source_file: str
    source_hash: str
    parser: str  # "mineru" | "plain_pdf" | "ocr" | "manual"
    parser_version: str
    pages: list[Page] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    raw_artifact_path: str | None = None
    # A non-fatal diagnostic from the parser: when no blocks could be extracted
    # this carries a precise reason ("encrypted" / "scanned" / "compressed") so
    # the runner can surface a meaningful error instead of a generic "no text".
    health_warning: str | None = None

    def block_by_id(self, block_id: str) -> Block | None:
        for b in self.blocks:
            if b.block_id == block_id:
                return b
        return None

    def section_by_id(self, section_id: str) -> Section | None:
        for s in self.sections:
            if s.section_id == section_id:
                return s
        return None

    def to_source_ref(self, block_id: str) -> SourceRef:
        """Build a SourceRef pointing at a block in this document."""
        blk = self.block_by_id(block_id)
        if blk is None:
            raise KeyError(f"unknown block {block_id}")
        return SourceRef(
            document_id=self.document_id,
            chunk_id=None,  # filled by chunker
            block_id=block_id,
            physical_page=blk.physical_page,
            printed_page=blk.printed_page,
            section_path=blk.section_path,
        )

    model_config = {"frozen": True}
