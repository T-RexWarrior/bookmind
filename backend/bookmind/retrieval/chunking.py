"""Structure-aware parent/child chunking for textbook retrieval."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

from ..domain.source_ref import SourceRef
from .chunk import DocumentChunk
from .parsed_document import Block, ParsedDocument


CHUNKER_VERSION = "chunker_v4"


def scope_chunks_to_book(chunks: list[DocumentChunk], book_id: str) -> list[DocumentChunk]:
    """Bind source-hash cached chunks to one concrete book record."""
    scoped: list[DocumentChunk] = []
    for chunk in chunks:
        raw_chunk_id = chunk.chunk_id
        if raw_chunk_id.startswith(f"{book_id}:"):
            raw_chunk_id = raw_chunk_id.split(":", 1)[1]
        elif ":" in raw_chunk_id:
            prior_scope, prior_raw_id = raw_chunk_id.split(":", 1)
            if prior_scope.startswith(("book_", "demo_")):
                raw_chunk_id = prior_raw_id
        chunk_id = f"{book_id}:{raw_chunk_id}"
        scoped.append(chunk.model_copy(update={
            "book_id": book_id, "chunk_id": chunk_id,
            "source_ref": chunk.source_ref.model_copy(update={"chunk_id": chunk_id}),
        }))
    return scoped


def estimate_tokens(text: str) -> int:
    """Conservative bilingual estimate used only when no model tokenizer exists."""
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    formula = len(re.findall(r"[=∑∫√±×÷≤≥{}_^]", text))
    latin_chars = sum(len(part) for part in re.findall(r"[A-Za-z0-9]+", text))
    punctuation = len(re.findall(r"[^\w\s\u3400-\u9fff]", text))
    return max(1, math.ceil((cjk + formula + latin_chars / 4 + punctuation / 3) * 1.2))


@dataclass(frozen=True)
class _Unit:
    text: str
    block: Block


class Chunker:
    """Create small retrieval children backed by section-level parents."""

    def __init__(self, target_tokens: int = 400, overlap_tokens: int = 50) -> None:
        if target_tokens <= 0 or overlap_tokens < 0 or overlap_tokens >= target_tokens:
            raise ValueError("need 0 <= overlap < target, target > 0")
        self.target_tokens = target_tokens
        self.overlap_tokens = overlap_tokens

    def chunk(self, doc: ParsedDocument, book_id: str) -> list[DocumentChunk]:
        chunks: list[DocumentChunk] = []
        for section_path, blocks in self._group_by_section(doc):
            chunks.extend(self._chunk_section(doc, book_id, section_path, blocks, len(chunks)))
        return chunks

    @staticmethod
    def _group_by_section(doc: ParsedDocument) -> list[tuple[tuple[str, ...], list[Block]]]:
        # Group contiguous runs, not every block with the same path globally.
        # Chapter-title pages recur in the appended exercise solutions; a
        # dictionary keyed only by section_path used to merge page 23 with page
        # 423 and create a single 400-page source range.
        groups: list[tuple[tuple[str, ...], list[Block]]] = []
        for block in sorted(doc.blocks, key=lambda b: b.reading_order):
            key = block.section_path or ("全文",)
            if not groups or groups[-1][0] != key:
                groups.append((key, []))
            groups[-1][1].append(block)
        return groups

    def _units(self, blocks: list[Block]) -> list[_Unit]:
        units: list[_Unit] = []
        hard_limit = max(self.target_tokens * 2, 64)
        for block in blocks:
            text = block.text.strip()
            if not text:
                continue
            if block.block_type == "table" and estimate_tokens(text) > self.target_tokens:
                rows = [line for line in text.splitlines() if line.strip()]
                header = rows[:2] if len(rows) >= 2 and set(rows[1].replace("|", "").strip()) <= {"-", ":", " "} else rows[:1]
                body = rows[len(header):]
                group: list[str] = []
                for row in body:
                    proposal = "\n".join([*header, *group, row])
                    if group and estimate_tokens(proposal) > self.target_tokens:
                        units.append(_Unit("\n".join([*header, *group]), block))
                        group = []
                    group.append(row)
                if group or not body:
                    units.append(_Unit("\n".join([*header, *group]), block))
                continue
            pieces = [text]
            if estimate_tokens(text) > hard_limit:
                pieces = [
                    piece.strip() for piece in re.split(r"(?<=[。！？.!?；;])\s*|\n+", text)
                    if piece.strip()
                ] or [text]
            for piece in pieces:
                if estimate_tokens(piece) <= hard_limit:
                    units.append(_Unit(piece, block))
                    continue
                chars_per_window = max(32, int(hard_limit / 1.2))
                for start in range(0, len(piece), chars_per_window):
                    units.append(_Unit(piece[start:start + chars_per_window], block))
        return units

    def _chunk_section(
        self, doc: ParsedDocument, book_id: str, section_path: tuple[str, ...],
        blocks: list[Block], start_index: int,
    ) -> list[DocumentChunk]:
        units = self._units(blocks)
        out: list[DocumentChunk] = []
        buffer: list[_Unit] = []
        char_cursor = 0
        buffer_start = 0
        occurrence = blocks[0].block_id if blocks else str(start_index)
        parent_digest = hashlib.sha1(
            ("/".join(section_path) + "|" + occurrence).encode("utf-8")
        ).hexdigest()[:10]
        parent_id = f"{doc.document_id}-parent-{parent_digest}"

        def emit(items: list[_Unit], char_start: int) -> None:
            if not items:
                return
            content = "\n".join(item.text for item in items)
            unique_blocks = list(dict.fromkeys(item.block.block_id for item in items))
            first = items[0].block
            page_start = min(item.block.physical_page for item in items)
            page_end = max(item.block.physical_page for item in items)
            chunk_id = f"{doc.document_id}-chk-{start_index + len(out)}"
            ref = SourceRef(
                document_id=doc.document_id, chunk_id=chunk_id,
                block_id=first.block_id, physical_page=page_start,
                printed_page=first.printed_page, section_path=section_path,
                char_range=(char_start, char_start + len(content)),
            )
            out.append(DocumentChunk(
                chunk_id=chunk_id, book_id=book_id, document_id=doc.document_id,
                section_id=self._section_id_for(doc, section_path),
                section_path=section_path, content=content, source_ref=ref,
                block_ids=unique_blocks, char_range=ref.char_range,
                parser_version=doc.parser_version, chunker_version=CHUNKER_VERSION,
                parent_chunk_id=parent_id, page_start=page_start, page_end=page_end,
            ))

        for unit in units:
            proposed = "\n".join(item.text for item in [*buffer, unit])
            heading_only = len(buffer) == 1 and buffer[0].block.block_type == "heading"
            if buffer and estimate_tokens(proposed) > self.target_tokens and not heading_only:
                emit(buffer, buffer_start)
                overlap: list[_Unit] = []
                overlap_size = 0
                for old in reversed(buffer):
                    size = estimate_tokens(old.text)
                    if overlap and overlap_size + size > self.overlap_tokens:
                        break
                    if size > self.overlap_tokens:
                        break
                    overlap.insert(0, old)
                    overlap_size += size
                buffer = overlap
                buffer_start = max(0, char_cursor - sum(len(x.text) + 1 for x in overlap))
            buffer.append(unit)
            char_cursor += len(unit.text) + 1
            if estimate_tokens("\n".join(item.text for item in buffer)) >= self.target_tokens:
                emit(buffer, buffer_start)
                buffer = []
                buffer_start = char_cursor
        emit(buffer, buffer_start)
        return out

    @staticmethod
    def _section_id_for(doc: ParsedDocument, path: tuple[str, ...]) -> str | None:
        for section in doc.sections:
            if section.section_path == path:
                return section.section_id
        return None


__all__ = ["CHUNKER_VERSION", "Chunker", "estimate_tokens", "scope_chunks_to_book"]
