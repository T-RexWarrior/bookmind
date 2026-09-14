"""MinerU parser adapter (OPEN_SOURCE_REFERENCES.md §6).

MinerU is the high-quality parsing path. This adapter has two modes:

1. **Local CLI** — when the ``mineru`` / ``magic-pdf`` command is installed it
   shells out, reads the official structured JSON output, and translates it to
   :class:`ParsedDocument` while preserving page numbers and layout blocks.
2. **Gateway model** — the USTC gateway exposes a ``mineru`` model that parses
   documents as a service; when configured, the adapter posts the file and
   reads the returned structure. This is used when the local CLI is absent.

When neither is available the adapter reports ``supports()==0`` and a failing
healthcheck, so ``select_parser`` cleanly falls back to :class:`PlainPdfFallback`
(ARCHITECTURE §4.1: "解析器可替换并能降级"). It never fabricates structure.

The gateway path requires a :class:`ModelRouter`-style caller to avoid a layering
cycle; we accept an injected ``gateway`` callable so this module depends only on
the interface, not on the router.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Callable

from .base import DocumentParser, FileMetadata, ParseOptions, ParserHealth
from ..parsed_document import Block, Page, ParsedDocument, Section


# A gateway callable: (endpoint, payload_bytes, api_key, timeout) -> (status, body_bytes).
Gateway = Callable[[str, bytes, str | None, float], tuple[int, bytes]]


class MinerUParser(DocumentParser):
    name = "mineru"
    version = "mineru_v1"

    def __init__(self, *, cli: str | None = None, gateway: Gateway | None = None,
                 api_key: str | None = None, base_url: str = "https://api.llm.ustc.edu.cn/v1") -> None:
        # Auto-detect the local CLI once at construction.
        self._cli = cli or shutil.which("mineru") or shutil.which("magic-pdf")
        self._gateway = gateway
        self._api_key = api_key or os.environ.get("USTC_LLM_API_KEY")
        self._base_url = base_url

    def supports(self, meta: FileMetadata) -> float:
        # Prefer MinerU strongly when we have a working local CLI or gateway.
        if not (meta.content_type == "application/pdf" or meta.filename.lower().endswith(".pdf")):
            return 0.0
        if self._cli is not None:
            return 0.95
        if self._gateway is not None and self._api_key:
            return 0.85
        return 0.0  # unavailable → let the fallback handle it

    def healthcheck(self) -> ParserHealth:
        if self._cli is not None:
            return ParserHealth(available=True, detail=f"cli={self._cli}")
        if self._gateway is not None and self._api_key:
            return ParserHealth(available=True, detail="gateway=mineru")
        return ParserHealth(available=False, detail="no local CLI and no gateway configured")

    def parse(self, source: bytes, meta: FileMetadata, options: ParseOptions | None = None) -> ParsedDocument:
        options = options or ParseOptions()
        if self._cli is not None:
            return self._parse_cli(source, meta, options)
        if self._gateway is not None and self._api_key:
            return self._parse_gateway(source, meta, options)
        raise RuntimeError("MinerUParser invoked but neither CLI nor gateway is configured")

    # --- local CLI path ----------------------------------------------------

    def _parse_cli(self, source: bytes, meta: FileMetadata, options: ParseOptions) -> ParsedDocument:
        """Shell out to the MinerU CLI and read its structured JSON output.

        The CLI writes ``<name>.md`` and a ``<name>_content_list.json`` (or
        ``auto/`` dir) describing blocks with page numbers and bbox. We parse
        the JSON list into our unified model. This is intentionally tolerant of
        minor schema differences between MinerU versions.
        """
        document_id = options.document_id or "doc-" + hashlib.sha256(source).hexdigest()[:12]
        source_hash = hashlib.sha256(source).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, meta.filename or "input.pdf")
            with open(in_path, "wb") as f:
                f.write(source)
            cmd = [self._cli, "-p", in_path, "-o", tmp]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
                # Surface the failure; do NOT fabricate a doc.
                raise RuntimeError(f"mineru CLI failed: {e}") from e

            content = _read_mineru_json(tmp)
            if content is None:
                raise RuntimeError("mineru CLI produced no parseable content_list")

        return _mineru_content_to_doc(content, document_id, meta.filename, source_hash, self.version)

    # --- gateway path ------------------------------------------------------

    def _parse_gateway(self, source: bytes, meta: FileMetadata, options: ParseOptions) -> ParsedDocument:
        """Post the file to the gateway ``mineru`` model and parse the result.

        The gateway contract here mirrors the documented campus API: a POST to
        ``/mineru`` (or ``/documents/parse``) with the file returns structured
        blocks. The exact endpoint is configurable; we default to ``/mineru``.
        """
        document_id = options.document_id or "doc-" + hashlib.sha256(source).hexdigest()[:12]
        source_hash = hashlib.sha256(source).hexdigest()
        status, body = self._gateway(  # type: ignore[misc]
            self._base_url + "/mineru", source, self._api_key, 300.0,
        )
        if status != 200:
            raise RuntimeError(f"mineru gateway failed: http {status}")
        try:
            data = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise RuntimeError(f"mineru gateway returned non-JSON: {e}") from e
        return _mineru_content_to_doc(data, document_id, meta.filename, source_hash, self.version)


# --- translation helpers --------------------------------------------------

def _read_mineru_json(out_dir: str) -> Any | None:
    """Locate and read MinerU's structured content list from an output dir."""
    for name in ("content_list.json", "_content_list.json"):
        path = os.path.join(out_dir, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    # Newer MinerU writes into an ``auto/`` subdirectory.
    auto = os.path.join(out_dir, "auto")
    if os.path.isdir(auto):
        for name in os.listdir(auto):
            if name.endswith("content_list.json"):
                with open(os.path.join(auto, name), "r", encoding="utf-8") as f:
                    return json.load(f)
    return None


def _mineru_content_to_doc(content: Any, document_id: str, filename: str,
                           source_hash: str, parser_version: str) -> ParsedDocument:
    """Translate MinerU's content_list (a list of block dicts) into ParsedDocument.

    Tolerates both the common shapes:
      - [{"type":"text","text":"...","page_idx":0}, ...]
      - [{"block_type":"text","text":"...","page_idx":0}, ...]
    Headings carry ``type``/``block_type`` in {"title","heading"} or a ``level``.
    """
    blocks: list[Block] = []
    pages: dict[int, Page] = {}
    sections: list[Section] = []
    current_path: tuple[str, ...] = ()
    order = 0

    if isinstance(content, dict) and "content_list" in content:
        content = content["content_list"]
    if not isinstance(content, list):
        content = []

    for item in content:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        page_idx = int(item.get("page_idx", item.get("page", 0)))
        physical_page = page_idx + 1
        btype_raw = str(item.get("type", item.get("block_type", "text"))).lower()
        is_heading = btype_raw in ("title", "heading", "h1", "h2", "h3")
        block_id = f"{document_id}-p{physical_page}-b{order}"
        if is_heading:
            current_path = (*current_path, text) if current_path else (text,)
            sid = f"{document_id}-sec-{len(sections)+1}"
            sections.append(Section(
                section_id=sid, title=text, section_path=current_path,
                physical_page=physical_page, block_ids=[block_id],
            ))
        blocks.append(Block(
            block_id=block_id, block_type="heading" if is_heading else "text",
            text=text, physical_page=physical_page,
            bbox=_bbox(item), reading_order=order, section_path=current_path,
        ))
        pages.setdefault(physical_page, Page(physical_page=physical_page, block_ids=[]))
        pages[physical_page].block_ids.append(block_id)  # type: ignore[union-attr]
        order += 1

    return ParsedDocument(
        document_id=document_id, source_file=filename, source_hash=source_hash,
        parser="mineru", parser_version=parser_version,
        pages=[pages[p] for p in sorted(pages)], sections=sections, blocks=blocks,
    )


def _bbox(item: dict) -> tuple[float, float, float, float] | None:
    for key in ("bbox", "box"):
        v = item.get(key)
        if isinstance(v, (list, tuple)) and len(v) == 4:
            return tuple(float(x) for x in v)  # type: ignore[return-value]
    return None
