"""SQLite / PostgreSQL repository — persists the domain across restarts
(PRODUCTIZATION M1, ARCHITECTURE §9).

Implements the :class:`~bookmind.storage.protocols.Repository` protocol on top
of SQLAlchemy 2.x. Domain ↔ ORM conversion happens here; the engine always
sees Pydantic domain models, never ORM rows.

Concurrency: ``save_state`` uses optimistic concurrency on ``state.version``
(``UPDATE ... WHERE version = ?``); a concurrent write raises
``ConcurrentWriteError``.

Idempotency: ``append_evidence`` relies on the ``event_key`` UNIQUE index — a
duplicate INSERT returns False (no write), matching ``InMemoryRepository``.

Retrieval artifacts (chunks, retriever) are NOT persisted in this slice
(POSTGRES/SQLite vector storage is M3). They live in process memory keyed by
book/project; the chunks are reproducible from the book's parsed document, so
a restart re-seeds them by re-running ingestion (the demo path does this
immediately). The ``chunks``/``retriever`` methods therefore behave exactly
like the in-memory impl within a single process.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from ...domain.enums import (
    BookRole,
    ConfidenceBand,
    Difficulty,
    EvidenceResult,
    EvidenceType,
    ExposureState,
    HintLevel,
    Level,
    LevelStatus,
    MisconceptionStatus,
    RelationType,
    SignalDirection,
    SignalStrength,
)
from ...domain.models import (
    Book,
    Concept,
    ConceptRelation,
    ContentBlock,
    Conversation,
    Evidence,
    LearnerConceptState,
    LevelRecord,
    LearningProject,
    Message,
    MisconceptionHypothesis,
    MisconceptionSignal,
    ProjectBook,
    Run,
    RunEvent,
    StateTransition,
    User,
)
from ...domain.source_ref import SourceRef
from ...jobs.job_store import IngestionJob, JobStage, JobState
from .models import (
    Base,
    BookRow,
    ConceptRelationRow,
    ConceptRow,
    ConversationRow,
    EvidenceRow,
    ExpiryKeyRow,
    IngestionJobRow,
    LearnerConceptStateRow,
    LearningProjectRow,
    MessageRow,
    MisconceptionHypothesisRow,
    ProjectBookRow,
    RunEventRow,
    RunRow,
    StateTransitionRow,
    SubmissionRow,
    TrustedTaskRow,
    UserRow,
)
from ..errors import ScopeError

if TYPE_CHECKING:
    from ...retrieval.chunk import DocumentChunk
    from ...retrieval.fusion import HybridRetriever


class ConcurrentWriteError(Exception):
    """Optimistic-concurrency conflict on ``save_state``."""


# ======================================================================
# Domain ↔ ORM conversion
# ======================================================================

def _src_refs_to_orm(refs: list[SourceRef]) -> list[dict]:
    return [r.model_dump(mode="json") for r in refs]


def _src_refs_from_orm(data: list[dict]) -> list[SourceRef]:
    return [SourceRef(**r) for r in data]


def _user_from_row(row: UserRow) -> User:
    return User(user_id=row.user_id, display_name=row.display_name)


def _project_from_row(row: LearningProjectRow) -> LearningProject:
    return LearningProject(
        project_id=row.project_id, learner_id=row.learner_id,
        name=row.name, goal=row.goal,
        learning_scope=getattr(row, "learning_scope", "") or "",
        deadline=getattr(row, "deadline", "") or "",
        current_plan=getattr(row, "current_plan", "") or "",
        last_source_id=getattr(row, "last_source_id", "") or "",
        last_source_page=getattr(row, "last_source_page", 1) or 1,
        default_mode=row.default_mode, created_at=row.created_at,
        updated_at=row.updated_at or row.created_at,
        last_activity_at=(getattr(row, "last_activity_at", None)
                          or row.updated_at or row.created_at),
        archived_at=row.deleted_at,
    )


def _book_from_row(row: BookRow) -> Book:
    return Book(
        book_id=row.book_id, owner_user_id=row.owner_user_id,
        source_hash=row.source_hash, title=row.title, edition=row.edition,
        parser_version=row.parser_version,
        source_type=getattr(row, "source_type", "PDF") or "PDF",
        original_filename=getattr(row, "original_filename", "") or "",
        page_count=getattr(row, "page_count", 0) or 0,
        section_count=getattr(row, "section_count", 0) or 0,
        outline=getattr(row, "outline", None) or [],
    )


def _concept_from_row(row: ConceptRow) -> Concept:
    return Concept(
        concept_id=row.concept_id, book_id=row.book_id, name=row.name,
        description=row.description, chapter=row.chapter, section=row.section,
        source_refs=_src_refs_from_orm(row.source_refs or []),
        importance=row.importance, difficulty=Difficulty(row.difficulty),
        source=row.source, prerequisites=row.prerequisites or [],
        related_concepts=row.related_concepts or [],
        goal_relevance=row.goal_relevance,
    )


def _concept_to_row(c: Concept) -> ConceptRow:
    return ConceptRow(
        concept_id=c.concept_id, book_id=c.book_id, name=c.name,
        description=c.description, chapter=c.chapter, section=c.section,
        source_refs=_src_refs_to_orm(c.source_refs), importance=c.importance,
        difficulty=c.difficulty.value, source=c.source,
        prerequisites=c.prerequisites, related_concepts=c.related_concepts,
        goal_relevance=c.goal_relevance,
    )


def _relation_from_row(row: ConceptRelationRow) -> ConceptRelation:
    return ConceptRelation(
        source_concept_id=row.source_concept_id,
        target_concept_id=row.target_concept_id,
        relation=RelationType(row.relation),
        source=row.source,
        rationale=row.rationale,
    )


def _relation_to_row(r: ConceptRelation) -> ConceptRelationRow:
    return ConceptRelationRow(
        source_concept_id=r.source_concept_id,
        target_concept_id=r.target_concept_id,
        relation=r.relation.value,
        source=r.source,
        rationale=r.rationale,
    )


def _signals_to_orm(signals: list[MisconceptionSignal]) -> list[dict]:
    return [s.model_dump(mode="json") for s in signals]


def _signals_from_orm(data: list[dict]) -> list[MisconceptionSignal]:
    out: list[MisconceptionSignal] = []
    for s in data or []:
        out.append(MisconceptionSignal(
            bug_id=s["bug_id"],
            direction=SignalDirection(s["direction"]),
            strength=SignalStrength(s["strength"]),
            reason=s.get("reason", ""),
        ))
    return out


def _evidence_from_row(row: EvidenceRow) -> Evidence:
    return Evidence(
        evidence_id=row.evidence_id, event_key=row.event_key,
        project_id=row.project_id, concept_id=row.concept_id,
        source_book_id=row.source_book_id,
        source_refs=_src_refs_from_orm(row.source_refs or []),
        source_chunk_ids=row.source_chunk_ids or [],
        evidence_type=EvidenceType(row.evidence_type),
        required_level=Level(row.required_level),
        result=EvidenceResult(row.result) if row.result else None,
        independent=row.independent, hint_level=HintLevel(row.hint_level),
        task_id=row.task_id, task_version=row.task_version,
        occurred_at=row.occurred_at, source_session=row.source_session,
        content_summary=row.content_summary,
        misconception_signals=_signals_from_orm(row.misconception_signals or []),
        correction_of_id=row.correction_of_id,
        discriminated_bug_ids=row.discriminated_bug_ids or [],
        scenario_fingerprint=row.scenario_fingerprint,
        high_discrimination=row.high_discrimination,
        scoring_type=row.scoring_type,
    )


def _evidence_to_row(e: Evidence) -> EvidenceRow:
    return EvidenceRow(
        evidence_id=e.evidence_id, event_key=e.event_key,
        project_id=e.project_id, concept_id=e.concept_id,
        source_book_id=e.source_book_id,
        source_refs=_src_refs_to_orm(e.source_refs),
        source_chunk_ids=e.source_chunk_ids,
        evidence_type=e.evidence_type.value,
        required_level=e.required_level.value,
        result=e.result.value if e.result else None,
        independent=e.independent, hint_level=int(e.hint_level),
        task_id=e.task_id, task_version=e.task_version,
        occurred_at=e.occurred_at, source_session=e.source_session,
        content_summary=e.content_summary,
        misconception_signals=_signals_to_orm(e.misconception_signals),
        correction_of_id=e.correction_of_id,
        discriminated_bug_ids=e.discriminated_bug_ids,
        scenario_fingerprint=e.scenario_fingerprint,
        high_discrimination=e.high_discrimination,
        scoring_type=e.scoring_type,
    )


def _levels_to_orm(levels: dict[str, LevelRecord]) -> dict:
    return {k: v.model_dump(mode="json") for k, v in levels.items()}


def _levels_from_orm(data: dict) -> dict[str, LevelRecord]:
    return {k: LevelRecord(**v) for k, v in (data or {}).items()}


def _state_from_row(row: LearnerConceptStateRow) -> LearnerConceptState:
    return LearnerConceptState(
        project_id=row.project_id, concept_id=row.concept_id,
        exposure_state=ExposureState(row.exposure_state),
        read_progress=row.read_progress,
        highest_ever_level=Level(row.highest_ever_level),
        current_verified_level=Level(row.current_verified_level),
        levels=_levels_from_orm(row.levels),
        goal_relevance=row.goal_relevance, version=row.version,
    )


def _state_to_row(s: LearnerConceptState) -> LearnerConceptStateRow:
    return LearnerConceptStateRow(
        project_id=s.project_id, concept_id=s.concept_id,
        exposure_state=s.exposure_state.value,
        read_progress=s.read_progress,
        highest_ever_level=s.highest_ever_level.value,
        current_verified_level=s.current_verified_level.value,
        levels=_levels_to_orm(s.levels),
        goal_relevance=s.goal_relevance, version=s.version,
    )


def _mis_from_row(row: MisconceptionHypothesisRow) -> MisconceptionHypothesis:
    return MisconceptionHypothesis(
        project_id=row.project_id, bug_id=row.bug_id,
        related_concepts=row.related_concepts or [],
        hypothesis_group=row.hypothesis_group,
        evidence_score=row.evidence_score,
        confidence_band=ConfidenceBand(row.confidence_band),
        status=MisconceptionStatus(row.status),
        evidence_ids=row.evidence_ids or [],
        alternatives=row.alternatives or [],
        remediation_version=row.remediation_version,
        changed_task_pass_count=row.changed_task_pass_count,
        changed_task_pass_fingerprints=row.changed_task_pass_fingerprints or [],
        hypothesis_cycle=row.hypothesis_cycle, updated_at=row.updated_at,
    )


def _mis_to_row(m: MisconceptionHypothesis) -> MisconceptionHypothesisRow:
    # ``m.status`` / ``m.confidence_band`` may be raw strings if a caller
    # mutated the field directly (Pydantic only coerces on construction);
    # normalise defensively.
    status = m.status.value if hasattr(m.status, "value") else str(m.status)
    band = m.confidence_band.value if hasattr(m.confidence_band, "value") else str(m.confidence_band)
    return MisconceptionHypothesisRow(
        project_id=m.project_id, bug_id=m.bug_id,
        related_concepts=m.related_concepts,
        hypothesis_group=m.hypothesis_group,
        evidence_score=m.evidence_score,
        confidence_band=band, status=status,
        evidence_ids=m.evidence_ids, alternatives=m.alternatives,
        remediation_version=m.remediation_version,
        changed_task_pass_count=m.changed_task_pass_count,
        changed_task_pass_fingerprints=m.changed_task_pass_fingerprints,
        hypothesis_cycle=m.hypothesis_cycle, updated_at=m.updated_at,
    )


def _transition_from_row(row: StateTransitionRow) -> StateTransition:
    return StateTransition(
        transition_id=row.transition_id, entity_type=row.entity_type,
        entity_id=row.entity_id, project_id=row.project_id,
        old_state=row.old_state, new_state=row.new_state,
        triggering_evidence_id=row.triggering_evidence_id,
        rule_version=row.rule_version, created_at=row.created_at,
    )


def _ingestion_job_to_row(job: IngestionJob) -> IngestionJobRow:
    return IngestionJobRow(
        job_id=job.job_id, project_id=job.project_id, book_id=job.book_id,
        source_hash=job.source_hash, filename=job.filename,
        state=job.state.value, stage=job.stage.value, progress=job.progress,
        attempt=job.attempt, max_attempts=job.max_attempts, error=job.error,
        parser=job.parser, parser_version=job.parser_version,
        chunker_version=job.chunker_version,
        embedding_model=job.embedding_model, embedding_dim=job.embedding_dim,
        parse_key=job.parse_key, chunk_key=job.chunk_key, index_key=job.index_key,
    )


def _ingestion_job_from_row(row: IngestionJobRow) -> IngestionJob:
    return IngestionJob(
        job_id=row.job_id, project_id=row.project_id, book_id=row.book_id,
        source_hash=row.source_hash, filename=row.filename,
        state=JobState(row.state), stage=JobStage(row.stage), progress=row.progress,
        attempt=row.attempt, max_attempts=row.max_attempts, error=row.error,
        parser=row.parser, parser_version=row.parser_version,
        chunker_version=row.chunker_version,
        embedding_model=row.embedding_model, embedding_dim=row.embedding_dim,
        parse_key=row.parse_key, chunk_key=row.chunk_key, index_key=row.index_key,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def _conversation_from_row(row: ConversationRow) -> Conversation:
    return Conversation(
        conversation_id=row.conversation_id, project_id=row.project_id,
        activity_type=row.activity_type or "LEARN", title=row.title,
        created_at=row.created_at, updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _message_from_row(row: MessageRow) -> Message:
    return Message(
        message_id=row.message_id, conversation_id=row.conversation_id,
        role=row.role,
        content_blocks=[ContentBlock(**block) for block in (row.content_blocks or [])],
        run_id=row.run_id, created_at=row.created_at,
    )


def _run_from_row(row: RunRow) -> Run:
    return Run(
        run_id=row.run_id, conversation_id=row.conversation_id,
        message_id=row.message_id, status=row.status, intent=row.intent,
        model=row.model, error=row.error, idempotency_key=row.idempotency_key,
        started_at=row.started_at, completed_at=row.completed_at,
        created_at=row.created_at,
    )


def _run_event_from_row(row: RunEventRow) -> RunEvent:
    return RunEvent(
        run_id=row.run_id, sequence=row.sequence, event_type=row.event_type,
        payload=row.payload or {}, created_at=row.created_at,
    )


# --- trusted task / submission dict ↔ row (M4) ---------------------------
# Stored as plain dicts to avoid coupling the storage layer to the task
# pipeline's Pydantic models; the TaskService owns dict↔domain conversion.

_TRUSTED_TASK_COLUMNS = (
    "task_id", "project_id", "conversation_id", "run_id", "learner_id",
    "task_version", "target_concept_ids", "evidence_for_levels", "rubric",
    "allowed_resources", "source_refs", "scenario_fingerprint", "is_probe",
    "discriminated_bug_ids", "is_changed_task", "remediation_stage",
    "prompt_text", "expected_answer", "distractors",
    "status", "hints_issued", "last_submission_id", "created_at", "expires_at",
)


def _trusted_task_from_row(row: TrustedTaskRow) -> dict:
    return {col: getattr(row, col) for col in _TRUSTED_TASK_COLUMNS}


def _trusted_task_to_row(d: dict) -> TrustedTaskRow:
    return TrustedTaskRow(**{col: d.get(col) for col in _TRUSTED_TASK_COLUMNS})


_SUBMISSION_COLUMNS = (
    "submission_id", "task_id", "project_id", "learner_id", "answer_text",
    "judgment", "run_id", "idempotency_key", "created_at",
)


def _submission_from_row(row: SubmissionRow) -> dict:
    return {col: getattr(row, col) for col in _SUBMISSION_COLUMNS}


def _submission_to_row(d: dict) -> SubmissionRow:
    return SubmissionRow(**{col: d.get(col) for col in _SUBMISSION_COLUMNS})


# ======================================================================
# Repository
# ======================================================================

class SqlRepository:
    """SQLAlchemy-backed Repository. Satisfies the ``Repository`` protocol."""

    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        # P1-13: normalize a bare ``postgresql://`` URL to the psycopg3 driver
        # scheme so docker-compose's DATABASE_URL works with the psycopg[binary]
        # dependency (SQLAlchemy's default ``postgresql://`` looks for psycopg2,
        # which is not installed). ``postgresql+psycopg://`` uses psycopg3.
        if database_url.startswith("postgresql://"):
            database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
        # SQLite needs check_same_thread=False for FastAPI's threadpool. For an
        # in-memory SQLite DB (:memory:) each connection gets its own private
        # database, so we must pin a single shared connection via StaticPool —
        # otherwise create_schema() and Session() see different databases.
        from sqlalchemy.pool import StaticPool
        connect_args: dict = {}
        pool_kwargs: dict = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            if ":memory:" in database_url:
                pool_kwargs = {"poolclass": StaticPool}
        self.engine = create_engine(
            database_url, echo=echo, future=True,
            connect_args=connect_args, **pool_kwargs,
        )
        self.Session = sessionmaker(self.engine, expire_on_commit=False, future=True)
        # Process-memory caches for non-DB artifacts (M3 will persist these).
        self._chunks: dict[str, list["DocumentChunk"]] = defaultdict(list)
        self._retrievers: dict[str, "HybridRetriever"] = {}
        # P0-05: a transaction boundary shared across multiple repo calls so the
        # Engine's submit_answer (evidence + state + misconception + transition)
        # commits atomically. When a transaction is active, every method reuses
        # its Session instead of opening+committing its own; commit happens once
        # at context exit. A failure rolls back every write in the same group.
        self._tx = threading.local()

    @contextmanager
    def transaction(self):
        """Group multiple repository calls into one DB transaction (P0-05).

        Within the ``with`` block, every method that would normally open its own
        Session instead reuses the bound Session, and no intermediate commit is
        issued. On clean exit the transaction commits once; on any exception it
        rolls back, so evidence + mastery state + misconception + transition
        are all-or-nothing. Nested calls reuse the outer transaction.
        """
        if getattr(self._tx, "session", None) is not None:
            # Already inside a transaction — reuse it, do not commit here.
            yield self._tx.session
            return
        session = self.Session()
        self._tx.session = session
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            self._tx.session = None
            session.close()

    def _session(self) -> Session:
        """The Session to use for this call: the active transaction's, or a new
        short-lived one (closed by the caller via ``with``)."""
        tx = getattr(self._tx, "session", None)
        if tx is not None:
            return tx
        return self.Session()

    def _own_session(self) -> tuple[Session, bool]:
        """Return (session, should_commit). When a transaction is active we
        reuse its session and the caller must NOT commit (the transaction
        commits once at context exit). Otherwise the caller owns the session
        and commits + closes it."""
        tx = getattr(self._tx, "session", None)
        if tx is not None:
            return tx, False
        return self.Session(), True

    # --- schema -----------------------------------------------------------

    def create_schema(self) -> None:
        """Create tables and apply the supported additive schema upgrade."""
        Base.metadata.create_all(self.engine)
        self._ensure_compat_columns()

    def _ensure_compat_columns(self) -> None:
        """Add the non-destructive learning-space columns to older databases.

        ``create_all`` intentionally does not alter existing tables.  The
        project is distributed as a zero-setup single-process app, so existing
        SQLite/PostgreSQL spaces receive these safe additive columns at startup.
        """
        existing = {
            table: {column["name"] for column in inspect(self.engine).get_columns(table)}
            for table in ("learning_projects", "books", "conversations")
        }
        project_columns = {
            "learning_scope": "TEXT DEFAULT ''",
            "deadline": "VARCHAR(32) DEFAULT ''",
            "current_plan": "TEXT DEFAULT ''",
            "last_source_id": "VARCHAR(64) DEFAULT ''",
            "last_source_page": "INTEGER DEFAULT 1",
            "last_activity_at": "DATETIME",
        }
        source_columns = {
            "source_type": "VARCHAR(32) DEFAULT 'PDF'",
            "original_filename": "VARCHAR(256) DEFAULT ''",
            "page_count": "INTEGER DEFAULT 0",
            "section_count": "INTEGER DEFAULT 0",
            "outline": "JSON DEFAULT '[]'",
        }
        conversation_columns = {
            "activity_type": "VARCHAR(16) DEFAULT 'LEARN' NOT NULL",
        }
        with self.engine.begin() as conn:
            for name, ddl in project_columns.items():
                if name not in existing["learning_projects"]:
                    conn.exec_driver_sql(f"ALTER TABLE learning_projects ADD COLUMN {name} {ddl}")
            for name, ddl in source_columns.items():
                if name not in existing["books"]:
                    conn.exec_driver_sql(f"ALTER TABLE books ADD COLUMN {name} {ddl}")
            for name, ddl in conversation_columns.items():
                if name not in existing["conversations"]:
                    conn.exec_driver_sql(f"ALTER TABLE conversations ADD COLUMN {name} {ddl}")

    def ping(self) -> None:
        """Open a trivial connection — used by /ready."""
        with self.engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")

    # --- identity / scoping ------------------------------------------------

    def add_user(self, user: User) -> None:
        with self.Session() as session:
            session.merge(UserRow(user_id=user.user_id, display_name=user.display_name))
            session.commit()

    def get_user(self, user_id: str) -> User | None:
        with self.Session() as session:
            row = session.get(UserRow, user_id)
            return _user_from_row(row) if row else None

    def create_project(self, project: LearningProject) -> None:
        with self.Session() as session:
            if session.get(UserRow, project.learner_id) is None:
                raise ScopeError(f"learner {project.learner_id} does not exist")
            if session.get(LearningProjectRow, project.project_id) is not None:
                raise ScopeError(f"project {project.project_id} already exists")
            session.add(LearningProjectRow(
                project_id=project.project_id, learner_id=project.learner_id,
                name=project.name, goal=project.goal,
                learning_scope=project.learning_scope,
                deadline=project.deadline, current_plan=project.current_plan,
                last_source_id=project.last_source_id,
                last_source_page=project.last_source_page,
                last_activity_at=project.last_activity_at,
                default_mode=project.default_mode.value,
            ))
            session.commit()

    def get_project(self, project_id: str) -> LearningProject | None:
        with self.Session() as session:
            row = session.get(LearningProjectRow, project_id)
            if row is None or row.deleted_at is not None:
                return None
            return _project_from_row(row)

    def projects_for_user(self, learner_id: str) -> list[LearningProject]:
        with self.Session() as session:
            rows = session.scalars(select(LearningProjectRow).where(
                LearningProjectRow.learner_id == learner_id,
                LearningProjectRow.deleted_at.is_(None),
            ).order_by(LearningProjectRow.created_at.desc()))
            return [_project_from_row(row) for row in rows]

    def assert_project_owned_by(self, project_id: str, learner_id: str) -> LearningProject:
        with self.Session() as session:
            row = session.get(LearningProjectRow, project_id)
            if row is None or row.deleted_at is not None:
                raise ScopeError(f"unknown project {project_id}")
            if row.learner_id != learner_id:
                raise ScopeError(f"project {project_id} not owned by {learner_id}")
            return _project_from_row(row)

    def archive_project(self, project_id: str) -> bool:
        """Soft-delete a project (sets deleted_at). Returns True if a live
        project was archived, False if it was already gone."""
        from datetime import datetime, timezone
        with self.Session() as session:
            row = session.get(LearningProjectRow, project_id)
            if row is None or row.deleted_at is not None:
                return False
            row.deleted_at = datetime.now(timezone.utc)
            session.commit()
            return True

    def update_project(self, project_id: str, *, name: str | None = None,
                       goal: str | None = None, learning_scope: str | None = None,
                       deadline: str | None = None, current_plan: str | None = None,
                       last_source_id: str | None = None,
                       last_source_page: int | None = None,
                       default_mode: "UIPreset | None" = None) -> None:
        with self.Session() as session:
            row = session.get(LearningProjectRow, project_id)
            if row is None or row.deleted_at is not None:
                raise ScopeError(f"unknown project {project_id}")
            if name is not None:
                row.name = name
            if goal is not None:
                row.goal = goal
            if learning_scope is not None:
                row.learning_scope = learning_scope
            if deadline is not None:
                row.deadline = deadline
            if current_plan is not None:
                row.current_plan = current_plan
            if last_source_id is not None:
                row.last_source_id = last_source_id
            if last_source_page is not None:
                row.last_source_page = max(1, last_source_page)
            if default_mode is not None:
                row.default_mode = default_mode.value
            row.last_activity_at = datetime.now(timezone.utc)
            session.commit()

    def get_project_mode(self, project_id: str) -> "UIPreset | None":
        from ...domain.enums import UIPreset
        with self.Session() as session:
            row = session.get(LearningProjectRow, project_id)
            if row is None or row.deleted_at is not None:
                return None
            try:
                return UIPreset(row.default_mode)
            except ValueError:
                return None

    # --- ingestion jobs ---------------------------------------------------

    def save_ingestion_job(self, job: IngestionJob) -> None:
        with self.Session() as session:
            session.merge(_ingestion_job_to_row(job))
            session.commit()

    def get_ingestion_job(self, job_id: str) -> IngestionJob | None:
        with self.Session() as session:
            row = session.get(IngestionJobRow, job_id)
            return _ingestion_job_from_row(row) if row else None

    def ingestion_jobs_for_project(self, project_id: str) -> list[IngestionJob]:
        with self.Session() as session:
            rows = session.scalars(select(IngestionJobRow).where(
                IngestionJobRow.project_id == project_id,
            ).order_by(IngestionJobRow.created_at.desc()))
            return [_ingestion_job_from_row(row) for row in rows]

    def latest_ingestion_job_for_book(self, book_id: str,
                                      project_id: str | None = None) -> IngestionJob | None:
        with self.Session() as session:
            statement = select(IngestionJobRow).where(IngestionJobRow.book_id == book_id)
            if project_id is not None:
                statement = statement.where(IngestionJobRow.project_id == project_id)
            row = session.scalars(statement.order_by(IngestionJobRow.created_at.desc())).first()
            return _ingestion_job_from_row(row) if row else None

    def recover_running_ingestion_jobs(self) -> list[IngestionJob]:
        recovered: list[IngestionJob] = []
        with self.Session() as session:
            rows = session.scalars(select(IngestionJobRow).where(
                IngestionJobRow.state == JobState.RUNNING.value,
            ))
            for row in rows:
                row.state = JobState.PENDING.value
                recovered.append(_ingestion_job_from_row(row))
            session.commit()
        return recovered

    def pending_ingestion_jobs(self) -> list[IngestionJob]:
        with self.Session() as session:
            rows = session.scalars(select(IngestionJobRow).where(
                IngestionJobRow.state == JobState.PENDING.value,
            ).order_by(IngestionJobRow.created_at))
            return [_ingestion_job_from_row(row) for row in rows]

    # --- conversations / runs --------------------------------------------

    def save_conversation(self, conversation: Conversation) -> None:
        with self.Session() as session:
            session.merge(ConversationRow(
                conversation_id=conversation.conversation_id,
                project_id=conversation.project_id,
                activity_type=conversation.activity_type,
                title=conversation.title,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
                deleted_at=conversation.deleted_at,
            ))
            session.commit()

    def conversations_for_project(self, project_id: str,
                                  activity_type: str | None = None) -> list[Conversation]:
        with self.Session() as session:
            statement = select(ConversationRow).where(
                ConversationRow.project_id == project_id,
                ConversationRow.deleted_at.is_(None),
            )
            if activity_type is not None:
                statement = statement.where(ConversationRow.activity_type == activity_type)
            rows = session.scalars(statement.order_by(ConversationRow.updated_at.desc()))
            return [_conversation_from_row(row) for row in rows]

    def get_conversation_record(self, conversation_id: str) -> Conversation | None:
        with self.Session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None or row.deleted_at is not None:
                return None
            return _conversation_from_row(row)

    def rename_conversation_record(self, conversation_id: str,
                                   title: str) -> Conversation | None:
        with self.Session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None or row.deleted_at is not None:
                return None
            row.title = title
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return _conversation_from_row(row)

    def delete_conversation_record(self, conversation_id: str) -> bool:
        with self.Session() as session:
            row = session.get(ConversationRow, conversation_id)
            if row is None or row.deleted_at is not None:
                return False
            row.deleted_at = datetime.now(timezone.utc)
            session.commit()
            return True

    def save_message(self, message: Message) -> None:
        with self.Session() as session:
            session.add(MessageRow(
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                role=message.role,
                content_blocks=[block.model_dump(mode="json") for block in message.content_blocks],
                run_id=message.run_id,
                created_at=message.created_at,
            ))
            session.commit()

    def messages_for_conversation(self, conversation_id: str) -> list[Message]:
        with self.Session() as session:
            rows = session.scalars(select(MessageRow).where(
                MessageRow.conversation_id == conversation_id,
            ).order_by(MessageRow.created_at))
            return [_message_from_row(row) for row in rows]

    def save_run_record(self, run: Run) -> None:
        with self.Session() as session:
            session.merge(RunRow(
                run_id=run.run_id, conversation_id=run.conversation_id,
                message_id=run.message_id, status=run.status, intent=run.intent,
                model=run.model, error=run.error,
                idempotency_key=run.idempotency_key,
                started_at=run.started_at, completed_at=run.completed_at,
                created_at=run.created_at,
            ))
            session.commit()

    def get_run_record(self, run_id: str) -> Run | None:
        with self.Session() as session:
            row = session.get(RunRow, run_id)
            return _run_from_row(row) if row else None

    def find_run_record(self, conversation_id: str, idempotency_key: str) -> Run | None:
        with self.Session() as session:
            row = session.scalars(select(RunRow).where(
                RunRow.conversation_id == conversation_id,
                RunRow.idempotency_key == idempotency_key,
            )).first()
            return _run_from_row(row) if row else None

    def save_run_event_records(self, events: list[RunEvent]) -> None:
        if not events:
            return
        with self.Session() as session:
            session.add_all([
                RunEventRow(
                    run_id=event.run_id, sequence=event.sequence,
                    event_type=event.event_type, payload=event.payload,
                    created_at=event.created_at,
                )
                for event in events
            ])
            session.commit()

    def run_event_records(self, run_id: str, *, after_sequence: int = -1) -> list[RunEvent]:
        with self.Session() as session:
            rows = session.scalars(select(RunEventRow).where(
                RunEventRow.run_id == run_id,
                RunEventRow.sequence > after_sequence,
            ).order_by(RunEventRow.sequence))
            return [_run_event_from_row(row) for row in rows]

    def add_book(self, book: Book) -> None:
        with self.Session() as session:
            session.merge(self._book_row(book))
            session.commit()

    def get_source(self, source_id: str) -> Book | None:
        with self.Session() as session:
            row = session.get(BookRow, source_id)
            return _book_from_row(row) if row else None

    def find_source_by_hash(self, learner_id: str, source_hash: str) -> Book | None:
        with self.Session() as session:
            row = session.scalars(select(BookRow).where(
                BookRow.owner_user_id == learner_id,
                BookRow.source_hash == source_hash,
            )).first()
            return _book_from_row(row) if row else None

    def update_source_metadata(self, source_id: str, *, parser_version: str | None = None,
                               page_count: int | None = None,
                               section_count: int | None = None,
                               outline: list[dict] | None = None) -> None:
        with self.Session() as session:
            row = session.get(BookRow, source_id)
            if row is None:
                raise ScopeError(f"unknown source {source_id}")
            if parser_version is not None:
                row.parser_version = parser_version
            if page_count is not None:
                row.page_count = page_count
            if section_count is not None:
                row.section_count = section_count
            if outline is not None:
                row.outline = list(outline)
            session.commit()

    def _book_row(self, book: Book) -> BookRow:
        return BookRow(
            book_id=book.book_id, owner_user_id=book.owner_user_id,
            source_hash=book.source_hash, title=book.title, edition=book.edition,
            parser_version=book.parser_version, source_type=book.source_type,
            original_filename=book.original_filename,
            page_count=book.page_count, section_count=book.section_count,
            outline=book.outline,
        )

    def link_book(self, pb: ProjectBook) -> None:
        with self.Session() as session:
            if pb.role.value == "PRIMARY":
                existing = session.scalars(select(ProjectBookRow).where(
                    ProjectBookRow.project_id == pb.project_id,
                    ProjectBookRow.role == "PRIMARY",
                )).first()
                if existing:
                    raise ScopeError(f"project {pb.project_id} already has a PRIMARY book")
            if session.scalars(select(ProjectBookRow).where(
                ProjectBookRow.project_id == pb.project_id,
                ProjectBookRow.book_id == pb.book_id,
            )).first():
                raise ScopeError(f"book {pb.book_id} already linked to project {pb.project_id}")
            session.add(ProjectBookRow(
                project_id=pb.project_id, book_id=pb.book_id, role=pb.role.value,
                enabled_for_retrieval=pb.enabled_for_retrieval, added_at=pb.added_at,
            ))
            session.commit()

    def _project_books(self, session: Session, project_id: str) -> list[ProjectBookRow]:
        return list(session.scalars(select(ProjectBookRow).where(
            ProjectBookRow.project_id == project_id,
        )))

    def allowed_book_ids(self, project_id: str, *, only_enabled: bool = True) -> set[str]:
        with self.Session() as session:
            ids: set[str] = set()
            for pb in self._project_books(session, project_id):
                if only_enabled and not pb.enabled_for_retrieval:
                    continue
                ids.add(pb.book_id)
            return ids

    def book_accessible_by(self, book_id: str, learner_id: str) -> bool:
        with self.Session() as session:
            book = session.get(BookRow, book_id)
            if book is None:
                return False
            if book.owner_user_id == learner_id:
                return True
            pbs = session.scalars(select(ProjectBookRow).where(ProjectBookRow.book_id == book_id))
            for pb in pbs:
                proj = session.get(LearningProjectRow, pb.project_id)
                if proj and proj.learner_id == learner_id:
                    return True
            return False

    # --- concepts ----------------------------------------------------------

    def add_concept(self, concept: Concept) -> None:
        with self.Session() as session:
            # Replace if exists (merge on book+concept unique).
            existing = session.scalars(select(ConceptRow).where(
                ConceptRow.book_id == concept.book_id,
                ConceptRow.concept_id == concept.concept_id,
            )).first()
            if existing:
                for col in ("name", "description", "chapter", "section", "source_refs",
                            "importance", "difficulty", "source", "prerequisites",
                            "related_concepts", "goal_relevance"):
                    setattr(existing, col, getattr(_concept_to_row(concept), col))
            else:
                session.add(_concept_to_row(concept))
            session.commit()

    def concepts_for_book(self, book_id: str) -> list[Concept]:
        with self.Session() as session:
            rows = session.scalars(select(ConceptRow).where(ConceptRow.book_id == book_id))
            return [_concept_from_row(r) for r in rows]

    def replace_book_graph(
        self, book_id: str, concepts: list[Concept], relations: list[ConceptRelation]
    ) -> None:
        """Replace concepts and edges for exactly one book in one transaction."""
        with self.Session() as session:
            old_ids = set(session.scalars(
                select(ConceptRow.concept_id).where(ConceptRow.book_id == book_id)
            ))
            new_ids = {c.concept_id for c in concepts}
            scoped_ids = old_ids | new_ids
            if scoped_ids:
                session.execute(delete(ConceptRelationRow).where(
                    ConceptRelationRow.source_concept_id.in_(scoped_ids)
                ))
            session.execute(delete(ConceptRow).where(ConceptRow.book_id == book_id))
            session.add_all(_concept_to_row(c) for c in concepts)
            session.add_all(_relation_to_row(r) for r in relations)
            session.commit()

    def relations_for_book(self, book_id: str) -> list[ConceptRelation]:
        with self.Session() as session:
            ids = set(session.scalars(
                select(ConceptRow.concept_id).where(ConceptRow.book_id == book_id)
            ))
            if not ids:
                return []
            rows = session.scalars(select(ConceptRelationRow).where(
                ConceptRelationRow.source_concept_id.in_(ids)
            ))
            return [_relation_from_row(r) for r in rows]

    def concept_in_project_scope(self, concept_id: str, project_id: str) -> bool:
        allowed = self.allowed_book_ids(project_id)
        with self.Session() as session:
            for bid in allowed:
                if session.scalars(select(ConceptRow).where(
                    ConceptRow.book_id == bid, ConceptRow.concept_id == concept_id,
                )).first():
                    return True
            return False

    # --- learner state -----------------------------------------------------

    def get_state(self, project_id: str, concept_id: str) -> LearnerConceptState:
        session, own = self._own_session()
        try:
            row = session.scalars(select(LearnerConceptStateRow).where(
                LearnerConceptStateRow.project_id == project_id,
                LearnerConceptStateRow.concept_id == concept_id,
            )).first()
            if row is None:
                return LearnerConceptState(project_id=project_id, concept_id=concept_id)
            return _state_from_row(row)
        finally:
            if own:
                session.close()

    def save_state(self, state: LearnerConceptState) -> None:
        session, own = self._own_session()
        try:
            row = session.scalars(select(LearnerConceptStateRow).where(
                LearnerConceptStateRow.project_id == state.project_id,
                LearnerConceptStateRow.concept_id == state.concept_id,
            )).first()
            if row is None:
                session.add(_state_to_row(state))
            else:
                # P0-05: optimistic concurrency. The caller bumped version; the
                # DB row must hold the prior version. A mismatch means another
                # write landed in between — reject with ConcurrentWriteError
                # instead of silently overwriting (the old code did `pass` and
                # clobbered the newer state, e.g. 0.1 overwriting 0.8).
                if state.version > 0 and row.version != state.version - 1:
                    raise ConcurrentWriteError(
                        f"state version conflict for {state.project_id}/{state.concept_id}: "
                        f"db={row.version} expected={state.version - 1} got={state.version}"
                    )
                new_row = _state_to_row(state)
                for col in ("exposure_state", "read_progress", "highest_ever_level",
                            "current_verified_level", "levels", "goal_relevance", "version"):
                    setattr(row, col, getattr(new_row, col))
            if own:
                session.commit()
        finally:
            if own:
                session.close()

    # --- evidence ----------------------------------------------------------

    def append_evidence(self, evidence: Evidence) -> bool:
        session, own = self._own_session()
        try:
            # Idempotency is on event_key only (matches InMemoryRepository).
            if session.scalars(select(EvidenceRow).where(
                EvidenceRow.event_key == evidence.event_key
            )).first():
                return False
            session.add(_evidence_to_row(evidence))
            if own:
                session.commit()
            return True
        finally:
            if own:
                session.close()

    def evidence_for(self, project_id: str, concept_id: str) -> list[Evidence]:
        with self.Session() as session:
            rows = session.scalars(select(EvidenceRow).where(
                EvidenceRow.project_id == project_id,
                EvidenceRow.concept_id == concept_id,
            ).order_by(EvidenceRow.occurred_at))
            return [_evidence_from_row(r) for r in rows]

    def evidence_for_misconception(self, project_id: str, bug_id: str) -> list[Evidence]:
        session, own = self._own_session()
        try:
            rows = session.scalars(select(EvidenceRow).where(
                EvidenceRow.project_id == project_id,
            ).order_by(EvidenceRow.occurred_at))
            out: list[Evidence] = []
            for r in rows:
                ev = _evidence_from_row(r)
                if any(s.bug_id == bug_id for s in ev.misconception_signals):
                    out.append(ev)
                elif ev.evidence_type.value == "CHANGED_TASK" and bug_id in (ev.discriminated_bug_ids or []):
                    out.append(ev)
            return out
        finally:
            if own:
                session.close()

    def evidence_for_project(self, project_id: str) -> list[Evidence]:
        with self.Session() as session:
            rows = session.scalars(select(EvidenceRow).where(
                EvidenceRow.project_id == project_id,
            ).order_by(EvidenceRow.occurred_at))
            return [_evidence_from_row(r) for r in rows]

    # --- misconceptions ----------------------------------------------------

    def get_misconception(self, project_id: str, bug_id: str) -> MisconceptionHypothesis | None:
        session, own = self._own_session()
        try:
            row = session.scalars(select(MisconceptionHypothesisRow).where(
                MisconceptionHypothesisRow.project_id == project_id,
                MisconceptionHypothesisRow.bug_id == bug_id,
            )).first()
            return _mis_from_row(row) if row else None
        finally:
            if own:
                session.close()

    def upsert_misconception(self, mis: MisconceptionHypothesis) -> None:
        session, own = self._own_session()
        try:
            row = session.scalars(select(MisconceptionHypothesisRow).where(
                MisconceptionHypothesisRow.project_id == mis.project_id,
                MisconceptionHypothesisRow.bug_id == mis.bug_id,
            )).first()
            if row is None:
                session.add(_mis_to_row(mis))
            else:
                new_row = _mis_to_row(mis)
                for col in ("related_concepts", "hypothesis_group", "evidence_score",
                            "confidence_band", "status", "evidence_ids", "alternatives",
                            "remediation_version", "changed_task_pass_count",
                            "changed_task_pass_fingerprints", "hypothesis_cycle", "updated_at"):
                    setattr(row, col, getattr(new_row, col))
            if own:
                session.commit()
        finally:
            if own:
                session.close()

    def all_misconceptions(self, project_id: str) -> list[MisconceptionHypothesis]:
        with self.Session() as session:
            rows = session.scalars(select(MisconceptionHypothesisRow).where(
                MisconceptionHypothesisRow.project_id == project_id,
            ))
            return [_mis_from_row(r) for r in rows]

    # --- transitions -------------------------------------------------------

    def record_transition(self, t: StateTransition) -> None:
        session, own = self._own_session()
        try:
            session.add(StateTransitionRow(
                transition_id=t.transition_id, entity_type=t.entity_type,
                entity_id=t.entity_id, project_id=t.project_id,
                old_state=t.old_state, new_state=t.new_state,
                triggering_evidence_id=t.triggering_evidence_id,
                rule_version=t.rule_version, created_at=t.created_at,
            ))
            if own:
                session.commit()
        finally:
            if own:
                session.close()

    def transitions_for(self, project_id: str, *, entity_type: str | None = None,
                        entity_id: str | None = None) -> list[StateTransition]:
        with self.Session() as session:
            statement = select(StateTransitionRow).where(
                StateTransitionRow.project_id == project_id,
            )
            if entity_type is not None:
                statement = statement.where(StateTransitionRow.entity_type == entity_type)
            if entity_id is not None:
                statement = statement.where(StateTransitionRow.entity_id == entity_id)
            rows = session.scalars(statement.order_by(StateTransitionRow.created_at))
            return [_transition_from_row(row) for row in rows]

    # --- expiry idempotency (LEARNING_MODEL §6) ---------------------------

    def record_expiry_key(self, key: str) -> None:
        session, own = self._own_session()
        try:
            session.merge(ExpiryKeyRow(key=key))
            if own:
                session.commit()
        finally:
            if own:
                session.close()

    def expiry_keys(self) -> set[str]:
        with self.Session() as session:
            return {row.key for row in session.scalars(select(ExpiryKeyRow))}

    # --- trusted tasks / submissions (M4) ----------------------------------

    def save_trusted_task(self, task_data: dict) -> None:
        with self.Session() as session:
            session.merge(_trusted_task_to_row(task_data))
            session.commit()

    def get_trusted_task(self, task_id: str) -> dict | None:
        with self.Session() as session:
            row = session.scalars(select(TrustedTaskRow).where(
                TrustedTaskRow.task_id == task_id,
            )).first()
            return _trusted_task_from_row(row) if row else None

    def pending_task_for_conversation(self, conversation_id: str) -> dict | None:
        with self.Session() as session:
            row = session.scalars(select(TrustedTaskRow).where(
                TrustedTaskRow.conversation_id == conversation_id,
                TrustedTaskRow.status == "PENDING",
            )).first()
            return _trusted_task_from_row(row) if row else None

    def pending_task_for_project(self, project_id: str) -> dict | None:
        with self.Session() as session:
            row = session.scalars(select(TrustedTaskRow).where(
                TrustedTaskRow.project_id == project_id,
                TrustedTaskRow.status == "PENDING",
            )).first()
            return _trusted_task_from_row(row) if row else None

    def update_task_status(self, task_id: str, status: str, *, last_submission_id: str | None = None) -> None:
        session, own = self._own_session()
        try:
            row = session.scalars(select(TrustedTaskRow).where(
                TrustedTaskRow.task_id == task_id,
            )).first()
            if row is None:
                return
            row.status = status
            if last_submission_id is not None:
                row.last_submission_id = last_submission_id
            if own:
                session.commit()
        finally:
            if own:
                session.close()

    def increment_task_hints(self, task_id: str) -> int:
        with self.Session() as session:
            row = session.scalars(select(TrustedTaskRow).where(
                TrustedTaskRow.task_id == task_id,
            )).first()
            if row is None:
                return 0
            row.hints_issued = int(row.hints_issued or 0) + 1
            session.commit()
            return int(row.hints_issued)

    def save_submission(self, submission_data: dict) -> bool:
        session, own = self._own_session()
        try:
            existing = session.scalars(select(SubmissionRow).where(
                SubmissionRow.task_id == submission_data.get("task_id", ""),
                SubmissionRow.idempotency_key == submission_data.get("idempotency_key", ""),
            )).first()
            if existing is not None:
                return False  # idempotent on (task_id, idempotency_key)
            session.add(_submission_to_row(submission_data))
            if own:
                session.commit()
            return True
        finally:
            if own:
                session.close()

    def get_submission_by_idem(self, task_id: str, idempotency_key: str) -> dict | None:
        with self.Session() as session:
            row = session.scalars(select(SubmissionRow).where(
                SubmissionRow.task_id == task_id,
                SubmissionRow.idempotency_key == idempotency_key,
            )).first()
            return _submission_from_row(row) if row else None

    # --- retrieval artifacts (process-memory until M3) --------------------

    def add_chunks(self, book_id: str, chunks: list["DocumentChunk"]) -> None:
        existing_ids = {c.chunk_id for c in self._chunks[book_id]}
        for c in chunks:
            if c.chunk_id not in existing_ids:
                self._chunks[book_id].append(c)
                existing_ids.add(c.chunk_id)

    def chunks_for_book(self, book_id: str) -> list["DocumentChunk"]:
        return list(self._chunks.get(book_id, []))

    def chunks_for_project(self, project_id: str, *, only_enabled: bool = True) -> list["DocumentChunk"]:
        out: list["DocumentChunk"] = []
        for bid in self.allowed_book_ids(project_id, only_enabled=only_enabled):
            out.extend(self.chunks_for_book(bid))
        return out

    def chunk_by_id(self, chunk_id: str) -> "DocumentChunk | None":
        for chunks in self._chunks.values():
            for c in chunks:
                if c.chunk_id == chunk_id:
                    return c
        return None

    def set_retriever(self, project_id: str, retriever: "HybridRetriever") -> None:
        self._retrievers[project_id] = retriever

    def get_retriever(self, project_id: str) -> "HybridRetriever | None":
        return self._retrievers.get(project_id)
