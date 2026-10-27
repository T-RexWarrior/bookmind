"""Original PDF pages and selectable text for a browser-compatible reader.

Local rendering only: no image model, OCR, or external service. The original
PDF and extracted learning text remain unchanged; page images stay in data/.
"""
from __future__ import annotations

import math
import threading
from pathlib import Path

import pymupdf
from fastapi import HTTPException

from . import learning, store as db

_render_lock = threading.Lock()
MAX_PIXELS = 12_000_000


def pdf_source(sid: str, mid: str, page: int | None = None):
    material = learning.require_material(sid, mid)
    if material["kind"] != "pdf" or not material["filename"]:
        raise HTTPException(404, "这份资料不是 PDF。")
    if page is not None and not 1 <= page <= material["page_count"]:
        raise HTTPException(404, "资料页码不存在。")
    directory = (db.DATA / "uploads").resolve()
    path = (directory / material["filename"]).resolve()
    if path.parent != directory or not path.is_file():
        raise HTTPException(404, "PDF 原文件不存在。")
    return material, path


def page_image(sid: str, mid: str, page: int, width: int) -> Path:
    material, source = pdf_source(sid, mid, page)
    directory = db.DATA / "pdf-pages" / material["sha"]
    target = directory / f"page-{page}-w{width}.png"
    if target.is_file():
        return target
    # Bound peak memory during quick page changes and concurrent image decodes.
    with _render_lock:
        if target.is_file():
            return target
        try:
            with pymupdf.open(source) as doc:
                pdf_page = doc[page - 1]
                rect = pdf_page.rect
                if rect.width <= 0 or rect.height <= 0:
                    raise ValueError("invalid page dimensions")
                scale = min(width / rect.width, math.sqrt(MAX_PIXELS / (rect.width * rect.height)))
                image = pdf_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
                data = image.tobytes("png")
            directory.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
            return target
        except Exception:
            raise HTTPException(422, "这页暂时无法绘制，请重试或切换到文字视图。") from None


def page_layout(sid: str, mid: str, page: int):
    _, source = pdf_source(sid, mid, page)
    try:
        with pymupdf.open(source) as doc:
            pdf_page = doc[page - 1]
            spans = []
            rotation = pdf_page.rotation_matrix
            # Exclude binary image payloads from this small text-only response.
            content = pdf_page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
            for block in content["blocks"]:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if not span["text"].strip():
                            continue
                        origin = pymupdf.Point(span["origin"]) * rotation
                        bbox = pymupdf.Rect(span["bbox"])
                        if bbox.width <= 0:
                            continue
                        spans.append({"text": span["text"], "x": origin.x, "y": origin.y, "width": bbox.width, "size": span["size"], "bold": bool(span["flags"] & 16)})
            return {"width": pdf_page.rect.width, "height": pdf_page.rect.height, "rotation": pdf_page.rotation, "spans": spans}
    except Exception:
        raise HTTPException(422, "暂时无法读取本页文字选区；仍可查看原版页面。") from None
