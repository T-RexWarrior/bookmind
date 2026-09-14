"""Built-in local OCR fallback for scanned and image-only PDFs.

RapidOCR supplies the Chinese/English text detector and recognizer while
pypdfium2 renders PDF pages.  The parser is deliberately lower priority than
MinerU and pypdf: ordinary text PDFs stay fast, and OCR is only paid for when
the higher-quality/cheaper parsers cannot produce usable blocks.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from .base import DocumentParser, FileMetadata, ParseOptions, ParserHealth
from ..parsed_document import Block, Page, ParsedDocument, Section


_HEADING = re.compile(
    r"^(?:第[一二三四五六七八九十百\d]+[编篇章节](?:\s+\S.*)?|"
    r"(?:chapter|part)\s+\d+|§\s*\d+(?:\.\d+){0,3}\s*\S.*|"
    r"\d+(?:\.\d+){0,3}\s+\S.{0,70})$",
    re.IGNORECASE,
)


class RapidOcrParser(DocumentParser):
    """Render each page and run local Chinese/English OCR."""

    name = "rapidocr"
    version = "rapidocr_v1"

    def __init__(
        self,
        *,
        engine_factory: Callable[[], Any] | None = None,
        render_scale: float = 1.4,
        min_score: float = 0.45,
    ) -> None:
        self._engine_factory = engine_factory
        self._render_scale = render_scale
        self._min_score = min_score

    @staticmethod
    def _available() -> bool:
        try:
            import onnxruntime  # noqa: F401
            import pypdfium2  # noqa: F401
            import rapidocr  # noqa: F401
        except ImportError:
            return False
        return True

    def supports(self, meta: FileMetadata) -> float:
        is_pdf = meta.content_type == "application/pdf" or meta.filename.lower().endswith(".pdf")
        if not is_pdf:
            return 0.0
        if self._engine_factory is None and not self._available():
            return 0.0
        # OCR is a fallback because it is slower and less structurally precise
        # than a real text layer or MinerU layout analysis.
        return 0.7

    def healthcheck(self) -> ParserHealth:
        available = self._engine_factory is not None or self._available()
        return ParserHealth(
            available=available,
            detail="RapidOCR + ONNX Runtime + PDFium ready" if available else "RapidOCR dependencies not installed",
        )

    def parse(self, source: bytes, meta: FileMetadata, options: ParseOptions | None = None) -> ParsedDocument:
        import pypdfium2 as pdfium

        options = options or ParseOptions()
        source_hash = hashlib.sha256(source).hexdigest()
        document_id = options.document_id or f"doc-{source_hash[:12]}"
        engine = self._engine_factory() if self._engine_factory else _new_engine()

        pages: list[Page] = []
        sections: list[Section] = []
        blocks: list[Block] = []
        current_path: tuple[str, ...] = ("全文",)
        current_section_index: int | None = None
        reading_order = 0

        pdf = pdfium.PdfDocument(source)
        try:
            for page_index in range(len(pdf)):
                pdf_page = pdf[page_index]
                bitmap = None
                page_block_ids: list[str] = []
                try:
                    width, height = pdf_page.get_size()
                    bitmap = pdf_page.render(scale=self._render_scale)
                    output = engine(bitmap.to_pil())
                    lines = _ocr_lines(output)
                finally:
                    if bitmap is not None:
                        bitmap.close()
                    pdf_page.close()

                for text, score, bbox in lines:
                    if score < self._min_score or len(text.strip()) < 2:
                        continue
                    text = re.sub(r"\s+", " ", text).strip()
                    is_heading = len(text) <= 90 and bool(_HEADING.match(text))
                    if is_heading:
                        current_path = _heading_path(current_path, text)
                        sections.append(Section(
                            section_id=f"{document_id}-sec-{len(sections) + 1}",
                            title=text,
                            section_path=current_path,
                            physical_page=page_index + 1,
                        ))
                        current_section_index = len(sections) - 1

                    block_id = f"{document_id}-p{page_index + 1}-b{reading_order}"
                    blocks.append(Block(
                        block_id=block_id,
                        block_type="heading" if is_heading else "text",
                        text=text,
                        physical_page=page_index + 1,
                        bbox=tuple(value / self._render_scale for value in bbox),
                        reading_order=reading_order,
                        section_path=current_path,
                    ))
                    page_block_ids.append(block_id)
                    if current_section_index is not None:
                        section = sections[current_section_index]
                        sections[current_section_index] = section.model_copy(
                            update={"block_ids": [*section.block_ids, block_id]}
                        )
                    reading_order += 1

                pages.append(Page(
                    physical_page=page_index + 1,
                    width=float(width),
                    height=float(height),
                    block_ids=page_block_ids,
                ))
        finally:
            pdf.close()

        if blocks and not sections:
            sections = [Section(
                section_id=f"{document_id}-sec-1",
                title="全文",
                section_path=("全文",),
                physical_page=1,
                block_ids=[block.block_id for block in blocks],
            )]

        return ParsedDocument(
            document_id=document_id,
            source_file=meta.filename,
            source_hash=source_hash,
            parser=self.name,
            parser_version=self.version,
            pages=pages,
            sections=sections,
            blocks=blocks,
            health_warning=None if blocks else "scanned: 本地中文 OCR 未识别到可用文字",
        )


def _new_engine() -> Any:
    from rapidocr import RapidOCR

    return RapidOCR()


def _ocr_lines(output: Any) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    """Normalize RapidOCR 2.x/3.x output and sort it in reading order."""
    boxes = getattr(output, "boxes", None)
    texts = getattr(output, "txts", None)
    scores = getattr(output, "scores", None)
    if texts is None and isinstance(output, (tuple, list)) and output:
        rows = output[0] or []
        normalized = []
        for row in rows:
            if len(row) >= 3:
                normalized.append((str(row[1]), float(row[2]), _rect(row[0])))
        return sorted(normalized, key=lambda item: (item[2][1], item[2][0]))
    if boxes is None or texts is None:
        return []
    if scores is None:
        scores = [1.0] * len(texts)
    normalized = [
        (str(text), float(score), _rect(box))
        for box, text, score in zip(boxes, texts, scores)
    ]
    return sorted(normalized, key=lambda item: (item[2][1], item[2][0]))


def _rect(box: Any) -> tuple[float, float, float, float]:
    points = list(box)
    if len(points) == 4 and all(isinstance(value, (int, float)) for value in points):
        return tuple(float(value) for value in points)  # type: ignore[return-value]
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _heading_path(current: tuple[str, ...], heading: str) -> tuple[str, ...]:
    match = re.match(r"^§?\s*(\d+(?:\.\d+)*)", heading)
    if match:
        depth = match.group(1).count(".") + 1
        return (*current[: depth - 1], heading)
    return (heading,)


__all__ = ["RapidOcrParser"]
