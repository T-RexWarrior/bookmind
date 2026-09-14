"""DocumentParser provider interface (OPEN_SOURCE_REFERENCES.md §5).

    DocumentParser
      supports(file_metadata) -> confidence
      parse(input, options) -> ParsedDocument
      healthcheck() -> ParserHealth

Implementations:
  - ``MinerUParser``  — official MinerU CLI/SDK adapter (Phase 2 real path)
  - ``PyPdfParser`` — fast local text-layer parser
  - ``RapidOcrParser`` — built-in local Chinese/English scanned-page fallback
  - ``PlainPdfFallback`` — dependency-free emergency fallback (offline/demo)

The ingestion pipeline ranks providers by ``supports()`` confidence and keeps
trying when a provider fails or yields no text. Downstream code depends only on
:class:`ParsedDocument`, never on parser internals.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class FileMetadata:
    """Lightweight metadata about an upload, used for parser selection."""

    filename: str
    content_type: str = "application/pdf"
    num_bytes: int = 0
    # Hint flags the uploader or a sniff step may set.
    is_scanned: bool | None = None
    has_text_layer: bool | None = None


@dataclass(frozen=True)
class ParserHealth:
    available: bool
    detail: str = ""


@dataclass(frozen=True)
class ParseOptions:
    """Options passed to ``parse`` — kept minimal and extensible."""

    document_id: str = ""
    save_raw_artifact: bool = True


class DocumentParser(ABC):
    """Abstract parser. Implementations must be idempotent on identical input."""

    name: str = "abstract"
    version: str = "0"

    @abstractmethod
    def supports(self, meta: FileMetadata) -> float:
        """Return a confidence in [0, 1] that this parser handles the file."""

    @abstractmethod
    def parse(self, source: bytes, meta: FileMetadata, options: ParseOptions | None = None) -> "ParsedDocument":
        """Parse raw bytes into a :class:`ParsedDocument`."""

    def healthcheck(self) -> ParserHealth:
        return ParserHealth(available=True, detail="ok")


def select_parser(parsers: list[DocumentParser], meta: FileMetadata) -> DocumentParser:
    """Pick the parser with the highest non-zero ``supports()`` confidence.

    Ties break on declaration order so a preferred parser listed first wins.
    """
    best: DocumentParser | None = None
    best_conf = 0.0
    for p in parsers:
        conf = p.supports(meta)
        if conf > best_conf:
            best = p
            best_conf = conf
    if best is None:
        raise RuntimeError(f"no parser supports {meta.filename}")
    return best


# Late import to avoid a cycle (ParsedDocument is defined in the parent package).
from ..parsed_document import ParsedDocument  # noqa: E402
