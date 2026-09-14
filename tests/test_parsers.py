"""L1 tests: document parsers — ARCHITECTURE.md §4.1, OPEN_SOURCE_REFERENCES §5/6.

Covers the provider interface, the plain-PDF fallback (deterministic, offline),
the MinerU adapter's selection/healthcheck behaviour, and graceful degradation.
"""

from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace

from bookmind.retrieval.parsers import (
    FileMetadata,
    MinerUParser,
    PlainPdfFallback,
    RapidOcrParser,
    select_parser,
)
from bookmind.retrieval.parsers.base import ParseOptions
from bookmind.retrieval.parsers.pypdf_parser import _cjk_text_layer_needs_ocr, _keep_outline_section


# A tiny synthetic PDF with a text-show operator so PlainPdfFallback has work.
_MINI_PDF = b"""%PDF-1.4
1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj
2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj
3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>endobj
4 0 obj<< /Length 60 >>stream
BT /F1 12 Tf 72 700 Td (3 Variables and Types) Tj 0 -14 Td (A variable names a storage location.) Tj ET
endstream
endobj
"""


def test_plain_pdf_supports_pdf():
    p = PlainPdfFallback()
    assert p.supports(FileMetadata("x.pdf")) == 0.3
    assert p.supports(FileMetadata("x.pdf", is_scanned=True)) == 0.1
    assert p.supports(FileMetadata("x.txt", content_type="text/plain")) == 0.0


def test_plain_pdf_extracts_blocks_with_pages():
    p = PlainPdfFallback()
    doc = p.parse(_MINI_PDF, FileMetadata("mini.pdf"), ParseOptions(document_id="d1"))
    assert doc.parser == "plain_pdf"
    assert doc.document_id == "d1"
    assert len(doc.blocks) >= 2
    assert all(b.physical_page == 1 for b in doc.blocks)
    # The first line is a heading-like "3 Variables and Types"
    headings = [b for b in doc.blocks if b.block_type == "heading"]
    assert headings, "expected at least one detected heading"
    assert any("Variables" in b.text for b in headings)
    # Source hash is stable for identical input.
    doc2 = p.parse(_MINI_PDF, FileMetadata("mini.pdf"), ParseOptions(document_id="d1"))
    assert doc2.source_hash == doc.source_hash
    assert doc2.blocks == doc.blocks  # deterministic


def test_plain_pdf_empty_on_no_streams():
    p = PlainPdfFallback()
    doc = p.parse(b"%PDF-1.4 no streams here", FileMetadata("empty.pdf"))
    assert doc.blocks == []
    assert doc.pages == []


def test_select_parser_prefers_mineru_when_available():
    mineru = MinerUParser(cli="/usr/bin/mineru")  # pretend-installed
    plain = PlainPdfFallback()
    meta = FileMetadata("book.pdf")
    chosen = select_parser([plain, mineru], meta)
    assert chosen is mineru


def test_select_parser_falls_back_when_mineru_unavailable():
    mineru = MinerUParser()  # no cli, no gateway
    plain = PlainPdfFallback()
    meta = FileMetadata("book.pdf")
    chosen = select_parser([mineru, plain], meta)
    assert chosen is plain


def test_mineru_healthcheck_unavailable_by_default(monkeypatch):
    monkeypatch.delenv("USTC_LLM_API_KEY", raising=False)
    m = MinerUParser()
    h = m.healthcheck()
    assert h.available is False
    assert m.supports(FileMetadata("x.pdf")) == 0.0


def test_mineru_gateway_path_translates_content(monkeypatch):
    # Fake gateway returns a MinerU-style content_list.
    content = [
        {"type": "title", "text": "Chapter 1 Variables", "page_idx": 0},
        {"type": "text", "text": "A variable names a storage location.", "page_idx": 0},
        {"type": "text", "text": "Primitives live on the stack.", "page_idx": 1},
    ]

    def fake_gateway(endpoint, payload, key, timeout):
        assert endpoint.endswith("/mineru")
        return 200, json.dumps(content).encode("utf-8")

    m = MinerUParser(gateway=fake_gateway, api_key="sk-test")
    assert m.supports(FileMetadata("x.pdf")) == 0.85
    doc = m.parse(b"%PDF-1.4 dummy", FileMetadata("x.pdf"), ParseOptions(document_id="d1"))
    assert doc.parser == "mineru"
    assert len(doc.blocks) == 3
    assert doc.blocks[0].block_type == "heading"
    assert doc.blocks[0].physical_page == 1
    assert doc.blocks[2].physical_page == 2  # page_idx 1 → physical 2
    assert len(doc.sections) == 1
    assert doc.sections[0].title == "Chapter 1 Variables"


def test_mineru_gateway_failure_raises(monkeypatch):
    def fake_gateway(endpoint, payload, key, timeout):
        return 500, b"error"
    m = MinerUParser(gateway=fake_gateway, api_key="sk-test")
    import pytest
    with pytest.raises(RuntimeError):
        m.parse(b"x", FileMetadata("x.pdf"))


def test_parsed_document_to_source_ref_roundtrip():
    p = PlainPdfFallback()
    doc = p.parse(_MINI_PDF, FileMetadata("mini.pdf"), ParseOptions(document_id="d1"))
    ref = doc.to_source_ref(doc.blocks[0].block_id)
    assert ref.document_id == "d1"
    assert ref.block_id == doc.blocks[0].block_id
    assert ref.physical_page == 1


def test_broken_chinese_hidden_text_layer_requires_ocr():
    mostly_latin_fragments = "int malloc return ElemType " * 500
    assert _cjk_text_layer_needs_ocr("数据结构C语言版.pdf", mostly_latin_fragments)
    assert not _cjk_text_layer_needs_ocr("english-data-structures.pdf", mostly_latin_fragments)
    assert not _cjk_text_layer_needs_ocr("数据结构.pdf", "线性表和链表的基本操作" * 300)


def test_rapidocr_extracts_image_only_pdf_with_page_geometry():
    from PIL import Image

    image = Image.new("RGB", (400, 240), "white")
    source = io.BytesIO()
    image.save(source, format="PDF")

    output = SimpleNamespace(
        boxes=[
            [[20, 20], [190, 20], [190, 50], [20, 50]],
            [[20, 75], [360, 75], [360, 105], [20, 105]],
        ],
        txts=["第一章 绪论", "数据结构用于组织和处理数据"],
        scores=[0.99, 0.98],
    )
    parser = RapidOcrParser(engine_factory=lambda: lambda image: output, render_scale=1.0)
    doc = parser.parse(source.getvalue(), FileMetadata("scan.pdf"), ParseOptions(document_id="ocr-doc"))

    assert doc.parser == "rapidocr"
    assert len(doc.pages) == 1
    assert doc.pages[0].width == 400
    assert doc.blocks[0].block_type == "heading"
    assert doc.blocks[0].physical_page == 1
    assert doc.blocks[1].text == "数据结构用于组织和处理数据"
    assert doc.blocks[1].bbox == (20.0, 75.0, 360.0, 105.0)


def test_outline_filter_keeps_navigation_and_drops_noise():
    assert _keep_outline_section("第1章 绪论", 1)
    assert _keep_outline_section("§1.2 复杂度度量", 2)
    assert _keep_outline_section("1.2 时间复杂度", 3)
    assert not _keep_outline_section("\uf06e 计算效率", 4)
    assert not _keep_outline_section("[8-1] 一道很长的课后习题", 2)
