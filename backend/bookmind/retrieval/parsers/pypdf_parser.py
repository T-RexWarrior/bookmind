"""Reliable local parser for ordinary PDFs with a text layer.

Unlike the regex-only emergency fallback, pypdf understands PDF object and
font encodings. It is intentionally local and deterministic; scanned PDFs are
reported as requiring OCR instead of producing invented text.
"""

from __future__ import annotations

import hashlib
import io
import re

from .base import DocumentParser, FileMetadata, ParseOptions, ParserHealth
from .quality import detect_printed_page, quality_label, score_page_text, summarize_page_quality
from ..parsed_document import Block, Page, ParsedDocument, Section


_HEADING = re.compile(
    r"^(?:第[一二三四五六七八九十百\d]+[编篇章节](?:\s+\S.*)?|(?:chapter|part)\s+\d+|\d+(?:\.\d+){0,3}\s+\S.{0,70})$",
    re.IGNORECASE,
)


class PyPdfParser(DocumentParser):
    name = "pypdf"
    version = "pypdf_v6"

    @staticmethod
    def _available() -> bool:
        try:
            import pypdf  # noqa: F401
        except ImportError:
            return False
        return True

    def supports(self, meta: FileMetadata) -> float:
        is_pdf = meta.content_type == "application/pdf" or meta.filename.lower().endswith(".pdf")
        if not is_pdf or not self._available() or meta.is_scanned is True:
            return 0.0
        return 0.8

    def healthcheck(self) -> ParserHealth:
        available = self._available()
        return ParserHealth(available=available, detail="pypdf ready" if available else "pypdf not installed")

    def parse(self, source: bytes, meta: FileMetadata, options: ParseOptions | None = None) -> ParsedDocument:
        from pypdf import PdfReader

        options = options or ParseOptions()
        document_id = options.document_id or "doc-" + hashlib.sha256(source).hexdigest()[:12]
        source_hash = hashlib.sha256(source).hexdigest()
        try:
            reader = PdfReader(io.BytesIO(source), strict=False)
        except Exception as exc:  # noqa: BLE001
            # Some legacy/simple PDF writers omit the xref/EOF table but still
            # contain readable text streams. Preserve support through the
            # conservative parser instead of rejecting useful course files.
            from .basic_pdf import PlainPdfFallback
            fallback = PlainPdfFallback().parse(source, meta, options)
            if fallback.blocks:
                return fallback
            raise RuntimeError(f"PDF 文件损坏或格式不受支持：{exc}") from exc
        if reader.is_encrypted:
            try:
                if reader.decrypt("") == 0:
                    raise RuntimeError("PDF 已加密，请解密后重新上传。")
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("PDF 已加密，请解密后重新上传。") from exc

        pages: list[Page] = []
        sections = _outline_sections(reader, document_id)
        has_outline = bool(sections)
        outline_order = sorted(range(len(sections)), key=lambda idx: (sections[idx].physical_page, idx))
        outline_cursor = -1
        outline_titles: dict[str, list[int]] = {}
        for idx, section in enumerate(sections):
            outline_titles.setdefault(_normalize_title(section.title), []).append(idx)
        blocks: list[Block] = []
        current_path: tuple[str, ...] = ("全文",)
        current_section_index: int | None = None
        order = 0

        selected_pages = set(options.page_numbers)
        total_pages = len(reader.pages)
        for page_no, pdf_page in enumerate(reader.pages, start=1):
            if options.is_cancelled and options.is_cancelled():
                raise RuntimeError("解析已取消")
            if selected_pages and page_no not in selected_pages:
                continue
            while (
                outline_cursor + 1 < len(outline_order)
                and sections[outline_order[outline_cursor + 1]].physical_page <= page_no
            ):
                outline_cursor += 1
            if outline_cursor >= 0:
                current_section_index = outline_order[outline_cursor]
                current_path = sections[current_section_index].section_path
            try:
                text = pdf_page.extract_text(extraction_mode="layout") or pdf_page.extract_text() or ""
            except TypeError:  # older pypdf without extraction_mode
                text = pdf_page.extract_text() or ""
            page_block_ids: list[str] = []
            printed_page = detect_printed_page(text, page_no)
            for paragraph in _paragraphs(text):
                normalized = _normalize_title(paragraph)
                outline_match = next(
                    (
                        idx for idx in outline_titles.get(normalized, [])
                        if sections[idx].physical_page == page_no
                    ),
                    None,
                )
                is_heading = outline_match is not None if has_outline else (
                    len(paragraph) <= 80 and bool(_HEADING.match(paragraph))
                )
                if outline_match is not None:
                    current_section_index = outline_match
                    current_path = sections[outline_match].section_path
                elif is_heading:
                    current_path = _heading_path(current_path, paragraph)
                    sections.append(Section(
                        section_id=f"{document_id}-sec-{len(sections) + 1}",
                        title=paragraph,
                        section_path=current_path,
                        physical_page=page_no,
                    ))
                    current_section_index = len(sections) - 1
                block_id = f"{document_id}-p{page_no}-b{order}"
                block = Block(
                    block_id=block_id,
                    block_type="heading" if is_heading else "text",
                    text=paragraph,
                    physical_page=page_no,
                    printed_page=printed_page,
                    reading_order=order,
                    section_path=current_path,
                    parser_name=self.name,
                    confidence=1.0,
                )
                blocks.append(block)
                page_block_ids.append(block_id)
                if current_section_index is not None:
                    sec = sections[current_section_index]
                    sections[current_section_index] = sec.model_copy(
                        update={"block_ids": [*sec.block_ids, block_id]}
                    )
                order += 1
            page_score, page_warnings = score_page_text(text, expected_text_page=True)
            label = quality_label(page_score)
            pages.append(Page(
                physical_page=page_no,
                printed_page=printed_page,
                width=float(pdf_page.mediabox.width),
                height=float(pdf_page.mediabox.height),
                block_ids=page_block_ids,
                parser_name=self.name,
                quality_score=page_score,
                quality_label=label,
                warning=None if label == "GOOD" else "；".join(page_warnings) or "原生文字层质量较低，建议执行 OCR",
            ))
            if options.on_page:
                options.on_page(page_no, total_pages, self.name)

        if blocks and not sections:
            sections = [Section(
                section_id=f"{document_id}-sec-1",
                title="全文",
                section_path=("全文",),
                physical_page=1,
                block_ids=[b.block_id for b in blocks],
            )]

        quality_summary = summarize_page_quality(pages)
        warnings = []
        if quality_summary.get("warning_pages", 0) or quality_summary.get("bad_pages", 0):
            warnings.append("部分页面的原生文字层质量较低，已交给自适应解析器复核。")

        return ParsedDocument(
            document_id=document_id,
            source_file=meta.filename,
            source_hash=source_hash,
            parser=self.name,
            parser_version=self.version,
            pages=pages,
            sections=sections,
            blocks=blocks,
            health_warning=None if blocks else "scanned: no text layer — OCR is required",
            pipeline_version="pipeline_v2",
            quality_summary=quality_summary,
            warnings=warnings,
        )


def _paragraphs(text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return [line for line in lines if len(line) >= 2]


def _heading_path(current: tuple[str, ...], heading: str) -> tuple[str, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)\s+", heading)
    if match:
        depth = match.group(1).count(".") + 1
        return (*current[: depth - 1], heading)
    return (heading,)


def _outline_sections(reader, document_id: str) -> list[Section]:
    """Convert PDF bookmarks into a clean section hierarchy when available."""
    sections: list[Section] = []
    try:
        outline = reader.outline
    except Exception:  # noqa: BLE001 — malformed outline is non-fatal
        return sections

    def visit(items, parent_path: tuple[str, ...] = ()) -> None:
        last_path = parent_path
        for item in items:
            if isinstance(item, list):
                visit(item, last_path)
                continue
            title = str(getattr(item, "title", "")).strip()
            if not title:
                continue
            try:
                physical_page = reader.get_destination_page_number(item) + 1
            except Exception:  # noqa: BLE001 — skip broken bookmark targets
                continue
            path = (*parent_path, title)
            last_path = path
            if not _keep_outline_section(title, len(path)):
                continue
            sections.append(Section(
                section_id=f"{document_id}-sec-{len(sections) + 1}",
                title=title,
                section_path=path,
                physical_page=physical_page,
            ))

    if isinstance(outline, list):
        visit(outline)
    return sections


def _normalize_title(text: str) -> str:
    return re.sub(r"\s+", "", text).strip("_-.·…")


def _keep_outline_section(title: str, depth: int) -> bool:
    """Keep navigational headings, not every bullet, exercise, or index term."""
    cleaned = title.strip()
    if not cleaned or cleaned.startswith(("\uf06e", "[")) or len(cleaned) > 120:
        return False
    if re.match(r"^(?:第[一二三四五六七八九十百\d]+章|§\s*\d+(?:\.\d+)*|chapter\s+\d+)", cleaned, re.I):
        return True
    if re.match(r"^\d+\.\d+\s+\S", cleaned):
        return True
    # Preserve short front matter and ordinary bookmark structures in PDFs
    # that do not use numbered Chinese headings.
    return depth <= 2


def _cjk_text_layer_needs_ocr(filename: str, text: str) -> bool:
    """Detect a broken hidden text layer in an expected Chinese document."""
    filename_has_cjk = any("\u4e00" <= char <= "\u9fff" for char in filename)
    if not filename_has_cjk:
        return False
    visible = sum(not char.isspace() for char in text)
    cjk = sum("\u4e00" <= char <= "\u9fff" for char in text)
    return visible >= 2000 and cjk < max(80, visible // 1000)


__all__ = ["PyPdfParser"]
