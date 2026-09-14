"""Source references — the provenance backbone of the learning workspace.

Everything that originates from a learning source carries a ``SourceRef`` so any
assertion can be traced back to document / page / block. The fields mirror
ARCHITECTURE.md §5 and LEARNING_MODEL.md §2.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    """A pointer into a parsed learning source.

    ``physical_page`` is 1-indexed and always present. ``printed_page`` is the
    page number *printed on the page* (which may differ from the physical
    position in a PDF, e.g. a roman-numeral preface) and is optional.
    """

    document_id: str
    chunk_id: str | None = None
    block_id: str | None = None
    physical_page: int
    printed_page: str | None = None
    section_path: tuple[str, ...] = ()
    char_range: tuple[int, int] | None = None

    model_config = {"frozen": True}

    def short_label(self) -> str:
        page = self.printed_page or str(self.physical_page)
        return f"p.{page}"
