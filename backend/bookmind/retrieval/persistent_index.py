"""Atomic single-machine persistence for keyword and dense textbook indexes."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

from ..llm.router import _tokenize
from .chunk import DocumentChunk


INDEX_VERSION = 2


def publish_index(
    root: Path, chunks: list[DocumentChunk], *, vectors: list[list[float]] | None,
    embedding_model: str = "",
) -> None:
    """Write complete temporary artifacts, then replace live files atomically."""
    root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    chunks_tmp = root / f"chunks.{token}.tmp"
    fts_tmp = root / f"keywords.{token}.tmp"
    meta_tmp = root / f"index_meta.{token}.tmp"
    vector_tmp = root / f"vectors.{token}.tmp"
    chunks_tmp.write_text(
        json.dumps([chunk.model_dump(mode="json") for chunk in chunks], ensure_ascii=False),
        encoding="utf-8",
    )
    connection = sqlite3.connect(fts_tmp)
    try:
        connection.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(chunk_id UNINDEXED, tokens, content UNINDEXED)")
        connection.executemany(
            "INSERT INTO chunks_fts(chunk_id, tokens, content) VALUES (?, ?, ?)",
            [(chunk.chunk_id, " ".join(_tokenize(chunk.content)), chunk.content) for chunk in chunks],
        )
        connection.commit()
    finally:
        connection.close()
    vector_ready = bool(vectors) and len(vectors or []) == len(chunks)
    dim = len(vectors[0]) if vector_ready and vectors else 0
    if vector_ready:
        import numpy as np
        with vector_tmp.open("wb") as handle:
            np.save(handle, np.asarray(vectors, dtype="float32"), allow_pickle=False)
    meta_tmp.write_text(json.dumps({
        "index_version": INDEX_VERSION,
        "embedding_model": embedding_model if vector_ready else "",
        "embedding_dim": dim,
        "vector_ready": vector_ready,
        "chunk_ids": [chunk.chunk_id for chunk in chunks],
    }, ensure_ascii=False), encoding="utf-8")
    os.replace(chunks_tmp, root / "chunks.json")
    os.replace(fts_tmp, root / "keywords.sqlite3")
    if vector_ready:
        os.replace(vector_tmp, root / "vectors.npy")
    os.replace(meta_tmp, root / "index_meta.json")


def load_vectors(root: Path) -> tuple[list[str], object | None, str]:
    """Load a read-only memory-mapped matrix and its exact embedding identity."""
    meta_path = root / "index_meta.json"
    vector_path = root / "vectors.npy"
    if not meta_path.is_file() or not vector_path.is_file():
        return [], None, ""
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
        if not meta.get("vector_ready"):
            return [], None, ""
        import numpy as np
        matrix = np.load(vector_path, mmap_mode="r", allow_pickle=False)
        ids = [str(value) for value in meta.get("chunk_ids", [])]
        if len(ids) != len(matrix):
            return [], None, ""
        return ids, matrix, str(meta.get("embedding_model") or "")
    except Exception:  # noqa: BLE001
        return [], None, ""


__all__ = ["INDEX_VERSION", "load_vectors", "publish_index"]
