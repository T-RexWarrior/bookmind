"""Ingestion jobs — ARCHITECTURE.md §11.

A single-worker, persistent, resumable pipeline. State machine::

    PENDING → RUNNING → SUCCEEDED
                  └→ RETRYABLE_FAILED → PENDING
                  └→ FAILED

Each stage writes a checkpoint so a crashed job resumes from the current
stage, not the start. Idempotency keys (ARCHITECTURE §4.1)::

    parse_key   = source_hash + parser_version
    chunk_key   = parse_key + chunker_version
    index_key   = chunk_key + embedding_model + embedding_dimension + index_version
    graph_key   = chunk_key + book_mapper_prompt_version + graph_rule_version

The same file is never re-processed if a successful derivation already exists
with a matching key (PRODUCT_SPEC §8 / ROADMAP Phase 2: "相同文件不重复处理").
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ..retrieval.chunk import DocumentChunk
from ..retrieval.parsed_document import ParsedDocument


class JobStage(str, Enum):
    QUEUED = "QUEUED"
    PARSING = "PARSING"
    CHUNKING = "CHUNKING"
    INDEXING = "INDEXING"
    GRAPH = "GRAPH"
    DONE = "DONE"


class JobState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class IngestionJob:
    job_id: str
    project_id: str
    book_id: str
    source_hash: str
    filename: str
    state: JobState = JobState.PENDING
    stage: JobStage = JobStage.QUEUED
    progress: float = 0.0  # 0..1
    attempt: int = 0
    max_attempts: int = 3
    error: str = ""
    parser: str = ""
    parser_version: str = ""
    chunker_version: str = ""
    embedding_model: str = ""
    embedding_dim: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Derived artifact keys, populated as stages complete.
    parse_key: str = ""
    chunk_key: str = ""
    index_key: str = ""

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)


def parse_key(source_hash: str, parser_version: str) -> str:
    raw = f"parse|{source_hash}|{parser_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def chunk_key(pkey: str, chunker_version: str) -> str:
    return hashlib.sha256(f"chunk|{pkey}|{chunker_version}".encode("utf-8")).hexdigest()[:16]


def index_key(ckey: str, embedding_model: str, embedding_dim: int, index_version: int = 1) -> str:
    raw = f"index|{ckey}|{embedding_model}|{embedding_dim}|{index_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class JobStore:
    """In-memory job table (a SQLite/migration-backed table later swaps in)."""

    def __init__(self) -> None:
        self._jobs: dict[str, IngestionJob] = {}
        # Cache of completed derivation keys → artifact, for idempotency.
        self._parse_cache: dict[str, ParsedDocument] = {}
        self._chunk_cache: dict[str, list[DocumentChunk]] = {}

    def submit(self, job: IngestionJob) -> None:
        self._jobs[job.job_id] = job

    def get(self, job_id: str) -> IngestionJob | None:
        return self._jobs.get(job_id)

    def all_jobs(self) -> list[IngestionJob]:
        return list(self._jobs.values())

    def jobs_for_project(self, project_id: str) -> list[IngestionJob]:
        return [j for j in self._jobs.values() if j.project_id == project_id]

    def recover_running(self) -> list[IngestionJob]:
        """On process restart, move leftover RUNNING jobs back to PENDING
        (ARCHITECTURE §11: "进程重启时把遗留 RUNNING 恢复为 PENDING")."""
        recovered = []
        for j in self._jobs.values():
            if j.state == JobState.RUNNING:
                j.state = JobState.PENDING
                j.stage = JobStage.QUEUED if j.stage == JobStage.QUEUED else j.stage
                j.touch()
                recovered.append(j)
        return recovered

    # --- idempotent artifact caches ---------------------------------------

    def has_parse(self, pkey: str) -> bool:
        return pkey in self._parse_cache

    def get_parse(self, pkey: str) -> ParsedDocument | None:
        return self._parse_cache.get(pkey)

    def put_parse(self, pkey: str, doc: ParsedDocument) -> None:
        self._parse_cache[pkey] = doc

    def has_chunks(self, ckey: str) -> bool:
        return ckey in self._chunk_cache

    def get_chunks(self, ckey: str) -> list[DocumentChunk] | None:
        return self._chunk_cache.get(ckey)

    def put_chunks(self, ckey: str, chunks: list[DocumentChunk]) -> None:
        self._chunk_cache[ckey] = chunks
