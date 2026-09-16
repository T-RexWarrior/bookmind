"""Optional adapter for a private PP-StructureV3 HTTP service.

The Windows package never requires this service.  When configured, the
adaptive parser sends only the difficult page numbers and accepts a small,
stable JSON representation, keeping PaddleOCR out of the desktop process.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import httpx

from .base import DocumentParser, FileMetadata, ParseOptions, ParserHealth
from .quality import quality_label, score_page_text, summarize_page_quality
from ..parsed_document import Block, Page, ParsedDocument, Section


class PpStructureParser(DocumentParser):
    name = "ppstructure_v3"
    version = "ppstructure_http_v1"

    def __init__(self, url: str = "", *, timeout: float = 300.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    def supports(self, meta: FileMetadata) -> float:
        is_pdf = meta.content_type == "application/pdf" or meta.filename.lower().endswith(".pdf")
        return 0.95 if is_pdf and self.url else 0.0

    def healthcheck(self) -> ParserHealth:
        if not self.url:
            return ParserHealth(False, "未配置私有 PP-StructureV3 服务")
        try:
            response = httpx.get(f"{self.url}/health", timeout=min(self.timeout, 3.0))
            return ParserHealth(response.is_success, f"HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            return ParserHealth(False, str(exc))

    def parse(self, source: bytes, meta: FileMetadata, options: ParseOptions | None = None) -> ParsedDocument:
        if not self.url:
            raise RuntimeError("未配置 PP-StructureV3 服务")
        options = options or ParseOptions()
        source_hash = hashlib.sha256(source).hexdigest()
        document_id = options.document_id or f"doc-{source_hash[:12]}"
        payload = {
            "document_id": document_id,
            "filename": meta.filename,
            "pages": list(options.page_numbers),
            "pdf_base64": base64.b64encode(source).decode("ascii"),
            "output": {"coordinates": True, "tables": "markdown", "formulas": "latex"},
        }
        response = httpx.post(f"{self.url}/parse", json=payload, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        return self._normalize(data, document_id, source_hash, meta.filename, options)

    def _normalize(
        self, data: dict[str, Any], document_id: str, source_hash: str,
        filename: str, options: ParseOptions,
    ) -> ParsedDocument:
        pages: list[Page] = []
        blocks: list[Block] = []
        sections: list[Section] = []
        current_path: tuple[str, ...] = ("全文",)
        total = int(data.get("page_count") or len(data.get("pages", [])))
        order = 0
        for raw_page in data.get("pages", []):
            page_no = int(raw_page.get("page") or raw_page.get("physical_page") or len(pages) + 1)
            page_ids: list[str] = []
            for raw in raw_page.get("blocks", []):
                text = str(raw.get("markdown") or raw.get("latex") or raw.get("text") or "").strip()
                kind = str(raw.get("type") or "text").lower()
                if kind not in {"text", "heading", "list", "code", "table", "formula", "image"}:
                    kind = "text"
                if kind == "heading" and text:
                    current_path = (*current_path[:-1], text) if current_path != ("全文",) else (text,)
                block_id = f"{document_id}-p{page_no}-pp-{order}"
                bbox = raw.get("bbox")
                block = Block(
                    block_id=block_id, block_type=kind, text=text,
                    physical_page=page_no, printed_page=raw_page.get("printed_page"),
                    bbox=tuple(float(v) for v in bbox) if bbox and len(bbox) == 4 else None,
                    reading_order=order, section_path=current_path,
                    parser_name=self.name,
                    confidence=max(0.0, min(1.0, float(raw.get("confidence", 0.9)))),
                )
                blocks.append(block)
                page_ids.append(block_id)
                if kind == "heading" and text:
                    sections.append(Section(
                        section_id=f"{document_id}-sec-pp-{len(sections) + 1}",
                        title=text, section_path=current_path, physical_page=page_no,
                        printed_page=raw_page.get("printed_page"), block_ids=[block_id],
                    ))
                order += 1
            page_text = "\n".join(b.text for b in blocks if b.physical_page == page_no)
            score, warnings = score_page_text(page_text, expected_text_page=True)
            confidence = raw_page.get("quality_score")
            if confidence is not None:
                score = max(0.0, min(1.0, float(confidence)))
            label = quality_label(score)
            pages.append(Page(
                physical_page=page_no, printed_page=raw_page.get("printed_page"),
                width=raw_page.get("width"), height=raw_page.get("height"),
                block_ids=page_ids, parser_name=self.name,
                quality_score=score, quality_label=label,
                warning=None if label == "GOOD" else "；".join(warnings) or "高精度解析结果仍需人工核对",
            ))
            if options.on_page:
                options.on_page(page_no, total, self.name)
        if blocks and not sections:
            sections = [Section(
                section_id=f"{document_id}-sec-pp-1", title="全文",
                section_path=("全文",), physical_page=min(p.physical_page for p in pages),
                block_ids=[b.block_id for b in blocks],
            )]
        return ParsedDocument(
            document_id=document_id, source_file=filename, source_hash=source_hash,
            parser=self.name, parser_version=self.version, pages=pages,
            sections=sections, blocks=blocks, pipeline_version="pipeline_v2",
            quality_summary=summarize_page_quality(pages),
        )


__all__ = ["PpStructureParser"]
