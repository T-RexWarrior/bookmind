"""SQLAlchemy ORM models — the persistence layer for the SQLite / PostgreSQL
repository (PRODUCTIZATION M1, ARCHITECTURE §9).

These tables mirror the domain models in ``domain/models.py``. The mapping is
deliberately explicit (no SQLModel magic): each table has a clear primary key,
the JSON-valued fields (``levels``, ``content_blocks``, ``misconception_signals``)
are stored as JSON columns, and append-only tables (``evidence``,
``state_transitions``, ``misconception_events``, ``run_events``) have no UPDATE
path in the repository.

Domain ↔ ORM conversion lives in ``repository.py``; these models never escape
the storage layer — the engine always sees Pydantic domain models.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for all BookMind ORM tables."""


# --- identity / scoping ---------------------------------------------------

class UserRow(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class LearningProjectRow(Base):
    __tablename__ = "learning_projects"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    learner_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), index=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    learning_scope: Mapped[str] = mapped_column(Text, default="")
    deadline: Mapped[str] = mapped_column(String(32), default="")
    current_plan: Mapped[str] = mapped_column(Text, default="")
    last_source_id: Mapped[str] = mapped_column(String(64), default="")
    last_source_page: Mapped[int] = mapped_column(Integer, default=1)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    default_mode: Mapped[str] = mapped_column(String(32), default="Quiet Reading")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Optimistic concurrency version.
    version: Mapped[int] = mapped_column(Integer, default=1)


class BookRow(Base):
    __tablename__ = "books"

    book_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), index=True)
    source_hash: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    edition: Mapped[str] = mapped_column(String(64), default="")
    parser_version: Mapped[str] = mapped_column(String(64), default="")
    source_type: Mapped[str] = mapped_column(String(32), default="PDF")
    original_filename: Mapped[str] = mapped_column(String(256), default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    section_count: Mapped[int] = mapped_column(Integer, default=0)
    outline: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ProjectBookRow(Base):
    __tablename__ = "project_books"
    __table_args__ = (UniqueConstraint("project_id", "book_id", name="uq_project_book"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("learning_projects.project_id"), index=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.book_id"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="PRIMARY")
    enabled_for_retrieval: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --- book graph -----------------------------------------------------------

class ConceptRow(Base):
    __tablename__ = "concepts"
    __table_args__ = (UniqueConstraint("book_id", "concept_id", name="uq_book_concept"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    concept_id: Mapped[str] = mapped_column(String(64), index=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("books.book_id"), index=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    chapter: Mapped[str] = mapped_column(String(128), default="")
    section: Mapped[str] = mapped_column(String(128), default="")
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    importance: Mapped[float] = mapped_column(Float, default=0.0)
    difficulty: Mapped[str] = mapped_column(String(16), default="MEDIUM")
    source: Mapped[str] = mapped_column(String(32), default="LLM_PROPOSED")
    prerequisites: Mapped[list] = mapped_column(JSON, default=list)
    related_concepts: Mapped[list] = mapped_column(JSON, default=list)
    goal_relevance: Mapped[float] = mapped_column(Float, default=0.0)


class ConceptRelationRow(Base):
    __tablename__ = "concept_relations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_concept_id: Mapped[str] = mapped_column(String(64), index=True)
    target_concept_id: Mapped[str] = mapped_column(String(64), index=True)
    relation: Mapped[str] = mapped_column(String(16), default="PREREQUISITE")
    source: Mapped[str] = mapped_column(String(32), default="LLM_PROPOSED")
    rationale: Mapped[str] = mapped_column(Text, default="")


# --- learner state (LEARNING_MODEL §3) ------------------------------------

class LearnerConceptStateRow(Base):
    __tablename__ = "learner_concept_states"
    __table_args__ = (UniqueConstraint("project_id", "concept_id", name="uq_state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("learning_projects.project_id"), index=True)
    concept_id: Mapped[str] = mapped_column(String(64), index=True)
    exposure_state: Mapped[str] = mapped_column(String(16), default="NONE")
    read_progress: Mapped[float] = mapped_column(Float, default=0.0)
    highest_ever_level: Mapped[str] = mapped_column(String(8), default="L0")
    current_verified_level: Mapped[str] = mapped_column(String(8), default="L0")
    # Per-level records: {level_value: {status, verified_at, stability_days, ...}}
    levels: Mapped[dict] = mapped_column(JSON, default=dict)
    goal_relevance: Mapped[float] = mapped_column(Float, default=0.0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


# --- evidence ledger (LEARNING_MODEL §4) ----------------------------------

class EvidenceRow(Base):
    __tablename__ = "evidence"

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("learning_projects.project_id"), index=True)
    concept_id: Mapped[str] = mapped_column(String(64), index=True)
    source_book_id: Mapped[str] = mapped_column(String(64), index=True)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    source_chunk_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence_type: Mapped[str] = mapped_column(String(16))
    required_level: Mapped[str] = mapped_column(String(8), default="L0")
    result: Mapped[str | None] = mapped_column(String(8), nullable=True)
    independent: Mapped[bool] = mapped_column(Boolean, default=False)
    hint_level: Mapped[int] = mapped_column(Integer, default=0)
    task_id: Mapped[str] = mapped_column(String(64), default="")
    task_version: Mapped[int] = mapped_column(Integer, default=0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    source_session: Mapped[str] = mapped_column(String(64), default="")
    content_summary: Mapped[str] = mapped_column(Text, default="")
    misconception_signals: Mapped[list] = mapped_column(JSON, default=list)
    correction_of_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discriminated_bug_ids: Mapped[list] = mapped_column(JSON, default=list)
    scenario_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    high_discrimination: Mapped[bool] = mapped_column(Boolean, default=False)
    scoring_type: Mapped[str] = mapped_column(String(32), default="")


class LearningMemoryRow(Base):
    """Project-scoped memory, kept distinct from the immutable evidence ledger."""

    __tablename__ = "learning_memories"

    memory_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("learning_projects.project_id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    concept_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


# --- misconception (LEARNING_MODEL §8) ------------------------------------

class MisconceptionHypothesisRow(Base):
    __tablename__ = "misconception_hypotheses"
    __table_args__ = (UniqueConstraint("project_id", "bug_id", name="uq_misconception"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("learning_projects.project_id"), index=True)
    bug_id: Mapped[str] = mapped_column(String(64), index=True)
    related_concepts: Mapped[list] = mapped_column(JSON, default=list)
    hypothesis_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_score: Mapped[int] = mapped_column(Integer, default=0)
    confidence_band: Mapped[str] = mapped_column(String(16), default="LOW")
    status: Mapped[str] = mapped_column(String(16), default="SUSPECTED")
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    alternatives: Mapped[list] = mapped_column(JSON, default=list)
    remediation_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changed_task_pass_count: Mapped[int] = mapped_column(Integer, default=0)
    changed_task_pass_fingerprints: Mapped[list] = mapped_column(JSON, default=list)
    hypothesis_cycle: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class StateTransitionRow(Base):
    __tablename__ = "state_transitions"

    transition_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    old_state: Mapped[str] = mapped_column(String(32), default="")
    new_state: Mapped[str] = mapped_column(String(32), default="")
    triggering_evidence_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rule_version: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --- expiry idempotency (LEARNING_MODEL §6) -------------------------------

class ExpiryKeyRow(Base):
    __tablename__ = "expiry_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)


# --- ingestion jobs (ARCHITECTURE §11; persisted for M3 recovery) ---------

class IngestionJobRow(Base):
    __tablename__ = "ingestion_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    book_id: Mapped[str] = mapped_column(String(64), index=True)
    source_hash: Mapped[str] = mapped_column(String(128), index=True)
    filename: Mapped[str] = mapped_column(String(256), default="")
    state: Mapped[str] = mapped_column(String(24), default="PENDING")
    stage: Mapped[str] = mapped_column(String(16), default="QUEUED")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error: Mapped[str] = mapped_column(Text, default="")
    parser: Mapped[str] = mapped_column(String(64), default="")
    parser_version: Mapped[str] = mapped_column(String(64), default="")
    chunker_version: Mapped[str] = mapped_column(String(64), default="")
    embedding_model: Mapped[str] = mapped_column(String(64), default="")
    embedding_dim: Mapped[int] = mapped_column(Integer, default=0)
    parse_key: Mapped[str] = mapped_column(String(64), default="")
    chunk_key: Mapped[str] = mapped_column(String(64), default="")
    index_key: Mapped[str] = mapped_column(String(64), default="")
    pages_done: Mapped[int] = mapped_column(Integer, default=0)
    pages_total: Mapped[int] = mapped_column(Integer, default=0)
    parser_mode: Mapped[str] = mapped_column(String(32), default="auto")
    quality_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    checkpoint_stage: Mapped[str] = mapped_column(String(64), default="")
    force_reparse: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


# --- conversations / messages / runs (M2) ---------------------------------

class ConversationRow(Base):
    __tablename__ = "conversations"

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("learning_projects.project_id"), index=True)
    activity_type: Mapped[str] = mapped_column(String(16), default="LEARN", index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class MessageRow(Base):
    __tablename__ = "messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    # Structured blocks: [{type:"text", text:...}, {type:"citation", ...}, ...]
    content_blocks: Mapped[list] = mapped_column(JSON, default=list)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id"), index=True)
    message_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")  # QUEUED|RUNNING|COMPLETED|FAILED|CANCELLED
    intent: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class RunEventRow(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_run_sequence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --- trusted tasks / submissions (M4 — real task & diagnosis closed loop) --

class TrustedTaskRow(Base):
    """A server-side, immutable task definition the browser can never override.

    Carries BOTH the TrustedTaskContext fields (what the Engine reads) and the
    TaskDraft-only fields (prompt_text/expected_answer/distractors) so the answer
    endpoint can render the prompt and the Diagnostician can read the rubric
    without the client ever echoing them back.
    """

    __tablename__ = "trusted_tasks"

    task_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("learning_projects.project_id"), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(64), default="")
    learner_id: Mapped[str] = mapped_column(String(64), index=True)

    # TrustedTaskContext
    task_version: Mapped[int] = mapped_column(Integer, default=1)
    target_concept_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence_for_levels: Mapped[list] = mapped_column(JSON, default=list)
    rubric: Mapped[list] = mapped_column(JSON, default=list)
    allowed_resources: Mapped[list] = mapped_column(JSON, default=list)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    scenario_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_probe: Mapped[bool] = mapped_column(Boolean, default=False)
    discriminated_bug_ids: Mapped[list] = mapped_column(JSON, default=list)
    is_changed_task: Mapped[bool] = mapped_column(Boolean, default=False)
    remediation_stage: Mapped[int] = mapped_column(Integer, default=0)

    # TaskDraft-only (not in TrustedTaskContext)
    prompt_text: Mapped[str] = mapped_column(Text, default="")
    expected_answer: Mapped[str] = mapped_column(Text, default="")
    distractors: Mapped[list] = mapped_column(JSON, default=list)

    # Lifecycle
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING|ANSWERED|EXPIRED|SKIPPED
    # A terminal task can enter a read-only follow-up phase.  It is kept
    # separate from status so the original answer outcome remains immutable.
    followup_open: Mapped[bool] = mapped_column(Boolean, default=False)
    hints_issued: Mapped[int] = mapped_column(Integer, default=0)
    last_submission_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SubmissionRow(Base):
    """One answer submission for a trusted task — append-only, idempotent on
    ``(task_id, idempotency_key)`` so a browser retry never double-writes."""

    __tablename__ = "submissions"
    __table_args__ = (UniqueConstraint("task_id", "idempotency_key", name="uq_task_idem"),)

    submission_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("trusted_tasks.task_id"), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    learner_id: Mapped[str] = mapped_column(String(64), index=True)
    answer_text: Mapped[str] = mapped_column(Text, default="")
    judgment: Mapped[dict] = mapped_column(JSON, default=dict)  # serialized AnswerJudgment
    run_id: Mapped[str] = mapped_column(String(64), default="")
    idempotency_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
