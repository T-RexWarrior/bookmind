"""Retrieval layer — document parsing, chunking, indexing and fusion.

This package implements the GenT-style Hybrid RAG pipeline (OPEN_SOURCE_REFERENCES
§7) and the OpenMAIC-style DocumentParser provider interface (§5), with all
downstream code depending only on the unified :class:`ParsedDocument` — never
on MinerU private fields (ARCHITECTURE §4.1).

    DocumentParser provider  →  ParsedDocument
    chunking                 →  DocumentChunk (leaf, with SourceRef)
    BM25 index + Dense index →  RRF fusion → optional rerank → Context Budget
    Citation Validator       →  grounds every answer in a real chunk
"""
