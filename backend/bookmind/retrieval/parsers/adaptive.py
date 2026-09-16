"""Page-level PDF quality routing for the BookMind v2 pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from .base import DocumentParser, FileMetadata, ParseOptions, ParserHealth
from .quality import summarize_page_quality
from .pypdf_parser import PyPdfParser
from .rapidocr_parser import RapidOcrParser
from .ppstructure import PpStructureParser
from ..parsed_document import Block, Page, ParsedDocument, Section


class AdaptivePdfParser(DocumentParser):
    """Use native text first, OCR only suspicious pages, then optional layout parsing."""

    name = "adaptive_pdf"
    version = "adaptive_pdf_v4"

    def __init__(
        self, native: DocumentParser | None = None, ocr: DocumentParser | None = None,
        high_precision: DocumentParser | None = None, *, batch_pages: int = 10,
    ) -> None:
        self.native = native or PyPdfParser()
        self.ocr = ocr or RapidOcrParser()
        self.high_precision = high_precision
        self.batch_pages = max(1, batch_pages)

    def supports(self, meta: FileMetadata) -> float:
        is_pdf = meta.content_type == "application/pdf" or meta.filename.lower().endswith(".pdf")
        return 1.0 if is_pdf else 0.0

    def healthcheck(self) -> ParserHealth:
        return ParserHealth(self.native.healthcheck().available or self.ocr.healthcheck().available, "逐页自适应解析")

    @property
    def config_fingerprint(self) -> str:
        parts = {
            "pipeline": self.version,
            "native": getattr(self.native, "version", ""),
            "ocr": getattr(self.ocr, "version", ""),
            "high": getattr(self.high_precision, "version", ""),
            "high_url": hashlib.sha256(
                str(getattr(self.high_precision, "url", "")).encode("utf-8")
            ).hexdigest()[:8],
            "ocr_scale": getattr(self.ocr, "_render_scale", None),
            "ocr_min_score": getattr(self.ocr, "_min_score", None),
            "batch": self.batch_pages,
        }
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]

    def parse(self, source: bytes, meta: FileMetadata, options: ParseOptions | None = None) -> ParsedDocument:
        options = options or ParseOptions()
        native = self.native.parse(source, meta, options)
        chosen: dict[int, tuple[Page, list[Block]]] = _page_map(native)
        suspect = [
            page.physical_page for page in native.pages
            if (page.quality_score or 0.0) < 0.80
        ]
        warnings = list(native.warnings)

        if suspect and self.ocr.supports(meta) > 0:
            try:
                ocr_doc = self.ocr.parse(source, meta, ParseOptions(
                    document_id=native.document_id, page_numbers=tuple(suspect),
                    on_page=options.on_page, is_cancelled=options.is_cancelled,
                    force_reparse=options.force_reparse,
                ))
                for page_no, candidate in _page_map(ocr_doc).items():
                    previous = chosen.get(page_no)
                    if previous is None or (candidate[0].quality_score or 0) > (previous[0].quality_score or 0):
                        chosen[page_no] = candidate
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"本地 OCR 不可用：{exc}")

        remaining = [
            page_no for page_no, (page, blocks) in chosen.items()
            if (page.quality_score or 0.0) < 0.45 or _complex_page(blocks)
        ]
        high_ready = False
        if remaining and self.high_precision and self.high_precision.supports(meta) > 0:
            health = self.high_precision.healthcheck()
            high_ready = health.available
            if not high_ready:
                warnings.append(f"私有高精度服务不可用：{health.detail}")
        if remaining and high_ready and self.high_precision:
            for start in range(0, len(remaining), self.batch_pages):
                batch = remaining[start:start + self.batch_pages]
                if options.is_cancelled and options.is_cancelled():
                    raise RuntimeError("解析已取消")
                try:
                    precise = self.high_precision.parse(source, meta, ParseOptions(
                        document_id=native.document_id, page_numbers=tuple(batch),
                        on_page=options.on_page, is_cancelled=options.is_cancelled,
                        force_reparse=options.force_reparse,
                    ))
                    for page_no, candidate in _page_map(precise).items():
                        previous = chosen.get(page_no)
                        # Layout-aware output wins on complex pages when it has text.
                        if candidate[1] and (
                            previous is None
                            or _complex_page(previous[1])
                            or (candidate[0].quality_score or 0) >= (previous[0].quality_score or 0)
                        ):
                            chosen[page_no] = candidate
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"私有高精度服务不可用，第 {batch[0]}～{batch[-1]} 页保留本地最佳结果：{exc}")

        chosen = _drop_repeated_margins(chosen)
        pages, blocks, sections = _rebuild(native, chosen)
        quality = summarize_page_quality(pages)
        if quality.get("warning_pages", 0) or quality.get("bad_pages", 0):
            warnings.append("部分页面识别质量可能较低，请结合原始 PDF 核对。")
        if options.on_page and pages:
            options.on_page(len(pages), len(pages), self.name)
        return ParsedDocument(
            document_id=native.document_id, source_file=native.source_file,
            source_hash=native.source_hash, parser=self.name, parser_version=self.version,
            pages=pages, sections=sections, blocks=blocks,
            health_warning=None if blocks else native.health_warning,
            pipeline_version="pipeline_v2", config_fingerprint=self.config_fingerprint,
            quality_summary=quality, warnings=list(dict.fromkeys(warnings)),
        )


def _page_map(doc: ParsedDocument) -> dict[int, tuple[Page, list[Block]]]:
    block_map = {block.block_id: block for block in doc.blocks}
    return {
        page.physical_page: (page, [block_map[bid] for bid in page.block_ids if bid in block_map])
        for page in doc.pages
    }


def _complex_page(blocks: list[Block]) -> bool:
    if any(block.block_type in {"table", "formula"} for block in blocks):
        return True
    text = "\n".join(block.text for block in blocks)
    table_like = len(re.findall(r"(?:\|.*\|)|(?:\S+\s{3,}\S+)", text)) >= 4
    formula_like = len(re.findall(r"[=∑∫√±×÷≤≥]|\\(?:frac|sum|int)", text)) >= 5
    return table_like or formula_like


def _rebuild(
    native: ParsedDocument, chosen: dict[int, tuple[Page, list[Block]]],
) -> tuple[list[Page], list[Block], list[Section]]:
    native_sections = sorted(native.sections, key=lambda s: (s.physical_page, len(s.section_path)))
    pages: list[Page] = []
    blocks: list[Block] = []
    section_blocks: dict[tuple[str, ...], list[str]] = {}
    order = 0
    for page_no in sorted(chosen):
        page, page_blocks = chosen[page_no]
        page_ids: list[str] = []
        default_path = ("全文",)
        for section in native_sections:
            if section.physical_page <= page_no:
                default_path = section.section_path
            else:
                break
        for index, old in enumerate(page_blocks):
            # OCR sees only a sparse subset of pages and therefore cannot keep
            # a trustworthy document-wide heading state. In particular, TOC
            # entries were once mistaken for headings and leaked into all
            # following OCR pages. Preserve the native PDF outline as the
            # structural authority while using OCR only for replacement text.
            if old.parser_name != native.parser:
                path = default_path
            else:
                path = old.section_path if old.section_path and old.section_path != ("全文",) else default_path
            block_id = f"{native.document_id}-p{page_no}-b{index}"
            block = old.model_copy(update={
                "block_id": block_id, "reading_order": order, "section_path": path,
            })
            blocks.append(block)
            page_ids.append(block_id)
            section_blocks.setdefault(path, []).append(block_id)
            order += 1
        pages.append(page.model_copy(update={"block_ids": page_ids}))
    sections: list[Section] = []
    seen: set[tuple[str, ...]] = set()
    title_pages: set[tuple[str, int]] = set()
    for original in native_sections:
        if original.section_path in seen:
            continue
        normalized = re.sub(r"\s+", "", original.title).casefold()
        if (normalized, original.physical_page) in title_pages:
            continue
        title_pages.add((normalized, original.physical_page))
        seen.add(original.section_path)
        sections.append(original.model_copy(update={"block_ids": section_blocks.get(original.section_path, [])}))
    for path, ids in section_blocks.items():
        if path not in seen:
            first = next(block for block in blocks if block.block_id == ids[0])
            sections.append(Section(
                section_id=f"{native.document_id}-sec-{len(sections) + 1}",
                title=path[-1], section_path=path,
                physical_page=first.physical_page, block_ids=ids,
            ))
    if blocks and not sections:
        sections = [Section(
            section_id=f"{native.document_id}-sec-1", title="全文",
            section_path=("全文",), physical_page=1,
            block_ids=[block.block_id for block in blocks],
        )]
    return pages, blocks, sections


def _drop_repeated_margins(
    chosen: dict[int, tuple[Page, list[Block]]],
) -> dict[int, tuple[Page, list[Block]]]:
    """Remove repeated short first/last lines that behave like headers/footers."""
    candidates: Counter[str] = Counter()
    for _, blocks in chosen.values():
        visible = [block for block in blocks if block.text.strip()]
        for block in [*visible[:2], *visible[-2:]]:
            key = re.sub(r"\s+", "", block.text).casefold()
            if 2 <= len(key) <= 80 and block.block_type != "heading":
                candidates[key] += 1
    threshold = max(3, int(len(chosen) * 0.30))
    repeated = {text for text, count in candidates.items() if count >= threshold}
    if not repeated:
        return chosen
    cleaned: dict[int, tuple[Page, list[Block]]] = {}
    for page_no, (page, blocks) in chosen.items():
        kept = [
            block for block in blocks
            if re.sub(r"\s+", "", block.text).casefold() not in repeated
        ]
        cleaned[page_no] = (page, kept)
    return cleaned


__all__ = ["AdaptivePdfParser"]
