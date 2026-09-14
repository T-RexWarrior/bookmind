"""Chunking — slice a ParsedDocument into leaf retrieval chunks.

ARCHITECTURE.md §4.1.1: chunks are cut *within a section*, preserving the title
path, adjacency and source position. Each chunk carries a :class:`SourceRef`
pointing back to document / page / block so citations can locate the original.

The chunker is deterministic and versioned (``chunker_version``); the ingestion
cache keys include it so re-chunking with a new version does not collide with
old chunks (ARCHITECTURE §4.1 ``chunk_key = parse_key + chunker_version``).
"""

from __future__ import annotations

from ..domain.source_ref import SourceRef
from ..llm.router import _tokenize
from .chunk import DocumentChunk
from .parsed_document import Block, ParsedDocument


CHUNKER_VERSION = "chunker_v2"


def scope_chunks_to_book(
    chunks: list[DocumentChunk], book_id: str,
) -> list[DocumentChunk]:
    """Bind source-hash cached chunks to one concrete book record.

    Parsed/chunk caches are deliberately shared by source hash, while ``book_id``
    is owner-specific.  Returning cached chunks verbatim therefore leaks the
    first uploader's id into later projects and makes the retrieval allowlist
    reject every chunk.  Give every materialized copy an owner-scoped id and
    keep ``SourceRef.chunk_id`` in sync.
    """
    scoped: list[DocumentChunk] = []
    for chunk in chunks:
        raw_chunk_id = chunk.chunk_id
        # Chunks written by this fix may themselves be reused from another
        # owner's persisted index. Strip exactly one prior book scope first.
        # The exact-prefix branch also keeps this operation idempotent for demo
        # and test ids that do not start with ``book_``.
        if raw_chunk_id.startswith(f"{book_id}:"):
            raw_chunk_id = raw_chunk_id.split(":", 1)[1]
        elif ":" in raw_chunk_id:
            prior_scope, prior_raw_id = raw_chunk_id.split(":", 1)
            if prior_scope.startswith(("book_", "demo_")):
                raw_chunk_id = prior_raw_id
        chunk_id = f"{book_id}:{raw_chunk_id}"
        source_ref = chunk.source_ref.model_copy(update={"chunk_id": chunk_id})
        scoped.append(chunk.model_copy(update={
            "book_id": book_id,
            "chunk_id": chunk_id,
            "source_ref": source_ref,
        }))
    return scoped


class Chunker:
    """Section-aware fixed-size chunker.

    Chunks never cross a section boundary. Within a section, blocks are
    concatenated in reading order and split into ~``target_tokens`` chunks,
    with ``overlap_tokens`` of overlap so phrase queries near a boundary still
    hit. A single block longer than the target is emitted as one oversized
    chunk rather than being split mid-sentence.
    """

    def __init__(self, target_tokens: int = 120, overlap_tokens: int = 24) -> None:
        if target_tokens <= 0 or overlap_tokens < 0 or overlap_tokens >= target_tokens:
            raise ValueError("need 0 <= overlap < target, target > 0")
        self.target_tokens = target_tokens
        self.overlap_tokens = overlap_tokens

    def chunk(self, doc: ParsedDocument, book_id: str) -> list[DocumentChunk]:
        chunks: list[DocumentChunk] = []
        # Group blocks by section_path (ordered by reading order).
        sections = self._group_by_section(doc)
        for section_path, blocks in sections:
            chunks.extend(self._chunk_section(
                doc, book_id, section_path, blocks, start_index=len(chunks),
            ))
        return chunks

    def _group_by_section(self, doc: ParsedDocument) -> list[tuple[tuple[str, ...], list[Block]]]:
        # Preserve first-appearance order; blocks already carry section_path.
        order: list[tuple[str, ...]] = []
        groups: dict[tuple[str, ...], list[Block]] = {}
        for b in doc.blocks:
            key = b.section_path
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(b)
        return [(k, groups[k]) for k in order]

    def _chunk_section(
        self,
        doc: ParsedDocument,
        book_id: str,
        section_path: tuple[str, ...],
        blocks: list[Block],
        *,
        start_index: int,
    ) -> list[DocumentChunk]:
        out: list[DocumentChunk] = []
        # Pre-tokenise each block so we can track token boundaries.
        tokenised = [(b, _tokenize(b.text)) for b in blocks]
        buf_tokens: list[str] = []
        buf_blocks: list[Block] = []
        buf_chars_start = 0
        running_chars = 0

        def flush() -> None:
            if not buf_tokens or not buf_blocks:
                return
            # Tokens are only for sizing. Retrieval content must preserve the
            # parser's real text; serialising token lists duplicated Chinese
            # text and inserted spaces between every character in v1.
            content = "\n".join(block.text.strip() for block in buf_blocks if block.text.strip())
            first = buf_blocks[0]
            chunk_id = f"{doc.document_id}-chk-{start_index + len(out)}"
            ref = SourceRef(
                document_id=doc.document_id,
                chunk_id=chunk_id,
                block_id=first.block_id,
                physical_page=first.physical_page,
                printed_page=first.printed_page,
                section_path=section_path,
                char_range=(buf_chars_start, buf_chars_start + len(content)),
            )
            chunk = DocumentChunk(
                chunk_id=chunk_id,
                book_id=book_id,
                document_id=doc.document_id,
                section_id=self._section_id_for(doc, section_path),
                section_path=section_path,
                content=content,
                source_ref=ref,
                block_ids=[b.block_id for b in buf_blocks],
                char_range=(buf_chars_start, buf_chars_start + len(content)),
                parser_version=doc.parser_version,
                chunker_version=CHUNKER_VERSION,
            )
            out.append(chunk)

        carried_only = False
        for block, toks in tokenised:
            if not toks:
                continue
            # If a single block already exceeds the target, flush the buffer and
            # emit the block as its own chunk (no mid-block split).
            if len(toks) >= self.target_tokens and buf_tokens:
                flush()
                buf_tokens, buf_blocks = [], []
                buf_chars_start = running_chars
                carried_only = False
            buf_tokens.extend(toks)
            buf_blocks.append(block)
            carried_only = False
            running_chars += len(" ".join(toks)) + 1
            if len(buf_tokens) >= self.target_tokens:
                flush()
                # Preserve overlap only at whole-block boundaries. Keeping a
                # slice of tokens would corrupt the displayed source text.
                tail = buf_blocks[-1:] if self.overlap_tokens and buf_blocks else []
                tail_tokens = _tokenize(tail[0].text) if tail else []
                if len(tail_tokens) <= self.overlap_tokens:
                    buf_blocks = tail
                    buf_tokens = tail_tokens
                    buf_chars_start = running_chars - len(tail[0].text)
                    carried_only = bool(tail)
                else:
                    buf_tokens, buf_blocks = [], []
                    buf_chars_start = running_chars
                    carried_only = False
        if not carried_only:
            flush()
        return out

    def _section_id_for(self, doc: ParsedDocument, path: tuple[str, ...]) -> str | None:
        if not path:
            return None
        for s in doc.sections:
            if s.section_path == path:
                return s.section_id
        return None
