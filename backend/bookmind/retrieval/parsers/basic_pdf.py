"""PlainPdfFallback — a pure-Python structural PDF parser (ARCHITECTURE §4.1).

This is the *offline* fallback parser. It has no third-party dependencies so
the demo corpus and CI run without MinerU or pypdf. It extracts text from a
PDF's content streams using a lightweight regex over ``Tj``/``TJ`` text-show
operators and groups the result into pages, sections and blocks with reading
order and page numbers.

Real-world PDFs vary enormously; this parser is deliberately conservative:
when it cannot extract meaningful text it signals ``parser="plain_pdf"`` with
empty blocks and a health warning, rather than fabricating structure
(OPEN_SOURCE_REFERENCES §6: "解析失败时明确提示，不静默生成错误教材结构").
For production-quality parsing the registry selects ``MinerUParser`` instead.
"""

from __future__ import annotations

import hashlib
import re

from .base import DocumentParser, FileMetadata, ParseOptions, ParserHealth
from ..parsed_document import Block, Page, ParsedDocument, Section


# Text-show operators inside a PDF content stream.
_TEXT_SHOW = re.compile(rb"\((.*?)\)\s*Tj", re.DOTALL)
# TJ array: [ (a) -10 (b) ] TJ  — concatenate strings with spaces.
_TJ_ARRAY = re.compile(rb"\[(.*?)\]\s*TJ", re.DOTALL)
# Page object boundaries are not trivially regex-able, so we split on the
# ``/Type /Page`` markers and the ``endstream`` boundaries as an approximation.
_PAGE_SPLIT = re.compile(rb"/Type\s*/Page[^s]")
# Stream content.
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
# Section heading heuristic: a short line that looks like "Chapter N" / "N.N Title".
_HEADING = re.compile(r"^(第[一二三四五六七八九十\d]+[章节](?:\s+\S.*)?|[0-9]+(\.[0-9]+)*\s+\S.{0,60})$")


def _decode_pdf_text(raw: bytes) -> str:
    """Best-effort decode of bytes inside a Tj/TJ operator to text."""
    try:
        return raw.decode("latin-1")
    except Exception:  # pragma: no cover
        return raw.decode("utf-8", errors="replace")


def _extract_streams(pdf_bytes: bytes) -> list[bytes]:
    """Return the content of each ``stream ... endstream`` block, in order.

    These are a superset of page-content streams (includes XObject forms), but
    for a structural fallback this is acceptable — we re-group by page markers.
    """
    return [m.group(1) for m in _STREAM.finditer(pdf_bytes)]


# FlateDecode (zlib) is the most common PDF stream filter. A stream whose
# dict declares /Filter /FlateDecode (or /Fl) is zlib-compressed and the raw
# bytes are not readable text until decompressed. Detect it so we can give a
# precise failure reason instead of silently extracting nothing and reporting
# "scanned or encrypted".
_FLATE_FILTER = re.compile(rb"/Filter\s*/FlateDecode|/Filter\s*/Fl\b")
# An encrypted PDF carries an /Encrypt entry in the trailer.
_ENCRYPTED = re.compile(rb"/Encrypt\s+")


def _has_flate_filter(pdf_bytes: bytes) -> bool:
    return bool(_FLATE_FILTER.search(pdf_bytes))


def _is_encrypted(pdf_bytes: bytes) -> bool:
    return bool(_ENCRYPTED.search(pdf_bytes))


def _decompress_zlib_streams(pdf_bytes: bytes) -> list[bytes]:
    """Best-effort zlib (FlateDecode) decompression of every stream block.

    PDF FlateDecode streams are raw zlib (no header byte for the predictive
    variant in the common case). We try zlib.decompress on each stream and keep
    the result when it succeeds; blocks that fail to decompress are skipped.
    This lets the fallback parser handle *compressed text* PDFs — the most
    common real-world case — without MinerU.
    """
    import zlib
    out: list[bytes] = []
    for m in _STREAM.finditer(pdf_bytes):
        raw = m.group(1)
        # Some writers pad with leading whitespace; strip it for decompression.
        candidate = raw.lstrip(b"\r\n\t ")
        try:
            out.append(zlib.decompress(candidate))
        except zlib.error:
            # Try a raw deflate (no zlib header) fallback.
            try:
                out.append(zlib.decompress(candidate, -15))
            except zlib.error:
                continue
    return out


def _text_from_stream(stream: bytes) -> str:
    """Extract text, joining Tj operators with newlines (each ~one line).

    Using newlines rather than spaces preserves line/paragraph boundaries so
    the downstream splitter can recover blocks; the PDF ``Td`` operator moves
    to a new line, and each ``Tj`` typically corresponds to one text fragment.
    """
    parts: list[str] = []
    for m in _TEXT_SHOW.finditer(stream):
        parts.append(_decode_pdf_text(m.group(1)))
    for m in _TJ_ARRAY.finditer(stream):
        inner = m.group(1)
        for s in re.finditer(rb"\(([^)]*)\)", inner):
            parts.append(_decode_pdf_text(s.group(1)))
    text = "\n".join(p for p in parts if p)
    # Collapse runs of spaces within a line, keep newlines as separators.
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


class PlainPdfFallback(DocumentParser):
    name = "plain_pdf"
    version = "plain_pdf_v1"

    def supports(self, meta: FileMetadata) -> float:
        # Low confidence: only used when nothing better is available, or when
        # the file has a text layer (no OCR needed) but MinerU is absent.
        if meta.content_type == "application/pdf" or meta.filename.lower().endswith(".pdf"):
            if meta.is_scanned:
                return 0.1  # weak on scanned pages
            return 0.3
        return 0.0

    def parse(self, source: bytes, meta: FileMetadata, options: ParseOptions | None = None) -> ParsedDocument:
        options = options or ParseOptions()
        document_id = options.document_id or _id_from_bytes(source)
        source_hash = hashlib.sha256(source).hexdigest()

        # Encrypted PDFs carry /Encrypt; we cannot read them without a password.
        if _is_encrypted(source):
            return ParsedDocument(
                document_id=document_id, source_file=meta.filename,
                source_hash=source_hash, parser=self.name, parser_version=self.version,
                health_warning="encrypted: this PDF is password-protected",
            )

        streams = _extract_streams(source)
        # If the raw streams yield no text and the file uses FlateDecode, the
        # streams are zlib-compressed. Try to decompress them before giving up
        # (the most common real-world PDF case). P0-03: a compressed text PDF
        # must not be misreported as "scanned or encrypted".
        text_streams = streams
        if streams and not any(_text_from_stream(s) for s in streams):
            if _has_flate_filter(source):
                decompressed = _decompress_zlib_streams(source)
                if any(_text_from_stream(s) for s in decompressed):
                    text_streams = decompressed

        pages: list[Page] = []
        blocks: list[Block] = []
        sections: list[Section] = []
        section_paths: dict[str, tuple[str, ...]] = {}

        if not text_streams:
            # No extractable text even after decompression. Distinguish a
            # scanned PDF (image-only, needs OCR) from a genuinely empty doc.
            reason = "scanned: no text layer — OCR is required"
            if _has_flate_filter(source):
                reason = "compressed: streams use FlateDecode but could not be decompressed; use MinerU or OCR"
            return ParsedDocument(
                document_id=document_id, source_file=meta.filename,
                source_hash=source_hash, parser=self.name, parser_version=self.version,
                health_warning=reason,
            )

        # Approximate page count from /Type /Page markers (≥1).
        page_markers = _PAGE_SPLIT.findall(source)
        num_pages = max(1, len(page_markers))
        # Distribute streams across pages (rough but sufficient for a fallback).
        per_page = max(1, len(text_streams) // num_pages) if text_streams else 1

        current_section_path: tuple[str, ...] = ()
        block_order = 0

        for pi in range(num_pages):
            physical_page = pi + 1
            page_block_ids: list[str] = []
            lo = pi * per_page
            hi = (lo + per_page) if pi < num_pages - 1 else len(text_streams)
            page_text = " ".join(_text_from_stream(s) for s in text_streams[lo:hi])

            if not page_text:
                page = Page(physical_page=physical_page, block_ids=[])
                pages.append(page)
                continue

            # Split into paragraphs as blocks; detect headings to build sections.
            for para in _split_paragraphs(page_text):
                para = para.strip()
                if not para:
                    continue
                block_id = f"{document_id}-p{physical_page}-b{block_order}"
                is_heading = bool(_HEADING.match(para)) and len(para) < 80
                if is_heading:
                    current_section_path = _extend_section_path(current_section_path, para)
                    section_id = f"{document_id}-sec-{len(sections)+1}"
                    sections.append(Section(
                        section_id=section_id, title=para,
                        section_path=current_section_path, physical_page=physical_page,
                        block_ids=[],
                    ))
                    section_paths[section_id] = current_section_path
                block = Block(
                    block_id=block_id,
                    block_type="heading" if is_heading else "text",
                    text=para, physical_page=physical_page,
                    reading_order=block_order, section_path=current_section_path,
                )
                blocks.append(block)
                page_block_ids.append(block_id)
                if sections and current_section_path:
                    sections[-1] = sections[-1].model_copy(update={"block_ids": sections[-1].block_ids + [block_id]})
                block_order += 1

            pages.append(Page(physical_page=physical_page, block_ids=page_block_ids))

        return ParsedDocument(
            document_id=document_id, source_file=meta.filename,
            source_hash=source_hash, parser=self.name, parser_version=self.version,
            pages=pages, sections=sections, blocks=blocks,
        )

    def healthcheck(self) -> ParserHealth:
        return ParserHealth(available=True, detail="plain_pdf fallback ready")


# --- helpers -------------------------------------------------------------

def _id_from_bytes(source: bytes) -> str:
    return "doc-" + hashlib.sha256(source).hexdigest()[:12]


def _split_paragraphs(text: str) -> list[str]:
    """Split page text into paragraph-ish blocks.

    Each Tj became one line; a single line is a block (rough paragraph). Blank
    lines are dropped. This is intentionally coarse — the fallback only needs
    to produce *some* addressable blocks, not perfect paragraphs.
    """
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    return parts


def _extend_section_path(current: tuple[str, ...], heading: str) -> tuple[str, ...]:
    """Maintain a section path, popping shallower levels when a new top section starts."""
    # Detect numeric depth: "3" → depth 1, "3.2" → depth 2, "3.2.1" → depth 3.
    m = re.match(r"^([0-9]+(?:\.[0-9]+)*)\s+(.+)$", heading)
    if m:
        depth = m.group(1).count(".") + 1
        truncated = current[: depth - 1]
        return (*truncated, heading)
    # Non-numeric heading (e.g. "第一章 ...") starts a new top section.
    return (heading,)
