"""Citation Validator — ARCHITECTURE.md §5, §4.2.

A *hard gate* (EVALUATION §8: "citation existence 100%"). For every citation in
a Tutor answer it verifies:

  1. the chunk_id belongs to the claimed book (and the project's allowlist);
  2. the cited text actually exists in that chunk;
  3. the page number maps to the chunk's source_ref;
  4. the citation came from the context the model was given (no hallucinated
     chunks).

On failure the spec is explicit: do *not* just drop the citation and keep the
claim. The system must either regenerate from supported chunks or, if that
also fails, state plainly that the textbook lacks sufficient basis
(PRODUCT_SPEC §8, ARCHITECTURE §4.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from .chunk import DocumentChunk


@dataclass
class CitationCheck:
    ok: bool
    chunk_id: str
    reason: str = ""
    chunk: DocumentChunk | None = None


@dataclass
class CitationReport:
    ok: bool  # True iff every citation passed
    checks: list[CitationCheck] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)
    reason: str = ""


class CitationValidator:
    """Validates citations against the context that was actually retrieved."""

    def __init__(self, chunks_by_id: dict[str, DocumentChunk], allowed_book_ids: set[str]) -> None:
        self.chunks_by_id = chunks_by_id
        self.allowed_book_ids = allowed_book_ids

    def validate(
        self,
        citations: list[dict],
        context_chunk_ids: list[str],
        answer_claims: list[str] | None = None,
    ) -> CitationReport:
        """Validate a list of citation dicts.

        Each citation: ``{"chunk_id": ..., "quote": ..., "page": ...}``.
        ``context_chunk_ids`` are the chunk_ids the model was given as context.
        """
        checks: list[CitationCheck] = []
        context_set = set(context_chunk_ids)
        for cite in citations:
            cid = cite.get("chunk_id", "")
            quote = cite.get("quote", "")
            page = cite.get("page")
            chunk = self.chunks_by_id.get(cid)
            if chunk is None:
                checks.append(CitationCheck(False, cid, f"chunk {cid} not in corpus"))
                continue
            if chunk.book_id not in self.allowed_book_ids:
                checks.append(CitationCheck(False, cid, f"chunk {cid} not in allowed books"))
                continue
            if cid not in context_set:
                checks.append(CitationCheck(False, cid, f"chunk {cid} not in the provided context"))
                continue
            if not isinstance(quote, str) or not quote.strip():
                checks.append(CitationCheck(False, cid, "quote is empty"))
                continue
            # PDF extraction inserts layout line breaks and spaces inside a
            # sentence. Accept only the same character sequence after
            # whitespace folding; punctuation and wording must still match.
            normalized_quote = re.sub(r"\s+", "", quote)
            normalized_content = re.sub(r"\s+", "", chunk.content)
            if quote not in chunk.content and normalized_quote not in normalized_content:
                checks.append(CitationCheck(False, cid, "quote not found in chunk content"))
                continue
            if page is not None:
                expected = chunk.source_ref.printed_page or str(chunk.source_ref.physical_page)
                if str(page) != expected:
                    checks.append(CitationCheck(False, cid, f"page {page} != chunk page {expected}"))
                    continue
            checks.append(CitationCheck(True, cid, "ok", chunk))

        all_ok = all(c.ok for c in checks) if checks else True
        reason = "" if all_ok else f"{sum(1 for c in checks if not c.ok)} citation(s) failed"
        return CitationReport(ok=all_ok, checks=checks, reason=reason)
