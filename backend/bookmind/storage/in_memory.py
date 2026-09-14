"""In-memory repository — the single persistence entry point.

ARCHITECTURE.md: "Repository 是唯一持久化入口". Agents and the Engine never
touch storage directly; they go through this interface. The in-memory impl
exists so the full closed loop is testable without a database; a SQLite
repository later swaps in behind the same protocol.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from contextlib import contextmanager
from typing import TYPE_CHECKING

from ..domain.models import (
    Book,
    Concept,
    ConceptRelation,
    Conversation,
    Evidence,
    LearnerConceptState,
    LearningProject,
    Message,
    MisconceptionHypothesis,
    ProjectBook,
    Run,
    RunEvent,
    StateTransition,
    User,
)
from ..jobs.job_store import IngestionJob, JobState
from .errors import ScopeError

if TYPE_CHECKING:
    from ..retrieval.chunk import DocumentChunk
    from ..retrieval.fusion import HybridRetriever


class InMemoryRepository:
    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.projects: dict[str, LearningProject] = {}
        self.books: dict[str, Book] = {}
        self.project_books: dict[str, list[ProjectBook]] = defaultdict(list)
        self.concepts: dict[str, list[Concept]] = defaultdict(list)  # book_id -> concepts
        self.relations: list[ConceptRelation] = []
        self.states: dict[tuple[str, str], LearnerConceptState] = {}
        self.evidence: list[Evidence] = []
        self._evidence_by_key: dict[str, Evidence] = {}
        self.misconceptions: dict[tuple[str, str], MisconceptionHypothesis] = {}
        self.transitions: list[StateTransition] = []
        # Idempotency set for derived expiry transitions (LEARNING_MODEL §6).
        self._expiry_keys: set[str] = set()
        # Retrieval artifacts: chunks per book, and a per-project retriever.
        self.chunks: dict[str, list["DocumentChunk"]] = defaultdict(list)  # book_id -> chunks
        self._retrievers: dict[str, "HybridRetriever"] = {}  # project_id -> retriever
        # Trusted tasks / submissions (M4). Plain dicts — the service converts
        # between these dicts and Pydantic domain models.
        self._trusted_tasks: dict[str, dict] = {}
        self._submissions: dict[str, dict] = {}
        self._submissions_by_idem: dict[tuple[str, str], dict] = {}
        self._ingestion_jobs: dict[str, IngestionJob] = {}
        self._conversations: dict[str, Conversation] = {}
        self._messages: dict[str, list[Message]] = defaultdict(list)
        self._runs: dict[str, Run] = {}
        self._run_events: dict[str, list[RunEvent]] = defaultdict(list)

    # --- transaction boundary (P0-05) -------------------------------------
    @contextmanager
    def transaction(self):
        """In-memory writes are already atomic within a single thread (dict
        mutations), so this is a no-op context manager. It exists so the Engine
        can call ``with repo.transaction():`` uniformly across both repos; the
        SQL repo uses it to bind a single Session and commit once."""
        yield

    # --- identity / scoping ------------------------------------------------

    def add_user(self, user: User) -> None:
        self.users[user.user_id] = user

    def get_user(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    def create_project(self, project: LearningProject) -> None:
        if project.learner_id not in self.users:
            raise ScopeError(f"learner {project.learner_id} does not exist")
        self.projects[project.project_id] = project

    def get_project(self, project_id: str) -> LearningProject | None:
        project = self.projects.get(project_id)
        return project if project is not None and project.archived_at is None else None

    def projects_for_user(self, learner_id: str) -> list[LearningProject]:
        return sorted(
            (p for p in self.projects.values()
             if p.learner_id == learner_id and p.archived_at is None),
            key=lambda p: p.created_at,
            reverse=True,
        )

    def assert_project_owned_by(self, project_id: str, learner_id: str) -> LearningProject:
        proj = self.projects.get(project_id)
        if proj is None:
            raise ScopeError(f"unknown project {project_id}")
        if proj.learner_id != learner_id:
            raise ScopeError(f"project {project_id} not owned by {learner_id}")
        return proj

    def archive_project(self, project_id: str) -> bool:
        """Soft-delete a project (sets archived_at). Returns True if a live
        project was archived, False if it was already gone."""
        from datetime import datetime, timezone
        proj = self.projects.get(project_id)
        if proj is None or proj.archived_at is not None:
            return False
        proj.archived_at = datetime.now(timezone.utc)
        return True

    def update_project(self, project_id: str, *, name: str | None = None,
                       goal: str | None = None, learning_scope: str | None = None,
                       deadline: str | None = None, current_plan: str | None = None,
                       last_source_id: str | None = None,
                       last_source_page: int | None = None,
                       default_mode: "UIPreset | None" = None) -> None:
        proj = self.projects.get(project_id)
        if proj is None:
            raise ScopeError(f"unknown project {project_id}")
        if name is not None:
            proj.name = name
        if goal is not None:
            proj.goal = goal
        if learning_scope is not None:
            proj.learning_scope = learning_scope
        if deadline is not None:
            proj.deadline = deadline
        if current_plan is not None:
            proj.current_plan = current_plan
        if last_source_id is not None:
            proj.last_source_id = last_source_id
        if last_source_page is not None:
            proj.last_source_page = max(1, last_source_page)
        if default_mode is not None:
            proj.default_mode = default_mode
        from datetime import datetime, timezone
        proj.updated_at = datetime.now(timezone.utc)
        proj.last_activity_at = proj.updated_at

    def get_project_mode(self, project_id: str) -> "UIPreset | None":
        proj = self.projects.get(project_id)
        return proj.default_mode if proj is not None else None

    # --- ingestion jobs ---------------------------------------------------

    def save_ingestion_job(self, job: IngestionJob) -> None:
        self._ingestion_jobs[job.job_id] = job

    def get_ingestion_job(self, job_id: str) -> IngestionJob | None:
        return self._ingestion_jobs.get(job_id)

    def ingestion_jobs_for_project(self, project_id: str) -> list[IngestionJob]:
        return sorted(
            (job for job in self._ingestion_jobs.values() if job.project_id == project_id),
            key=lambda job: job.created_at,
            reverse=True,
        )

    def latest_ingestion_job_for_book(self, book_id: str,
                                      project_id: str | None = None) -> IngestionJob | None:
        matches = [
            job for job in self._ingestion_jobs.values()
            if job.book_id == book_id and (project_id is None or job.project_id == project_id)
        ]
        return max(matches, key=lambda job: job.created_at) if matches else None

    def recover_running_ingestion_jobs(self) -> list[IngestionJob]:
        recovered: list[IngestionJob] = []
        for job in self._ingestion_jobs.values():
            if job.state == JobState.RUNNING:
                job.state = JobState.PENDING
                job.touch()
                recovered.append(job)
        return recovered

    def pending_ingestion_jobs(self) -> list[IngestionJob]:
        return [job for job in self._ingestion_jobs.values() if job.state == JobState.PENDING]

    # --- conversations / runs --------------------------------------------

    def save_conversation(self, conversation: Conversation) -> None:
        self._conversations[conversation.conversation_id] = conversation

    def conversations_for_project(self, project_id: str,
                                  activity_type: str | None = None) -> list[Conversation]:
        return sorted(
            (
                conversation for conversation in self._conversations.values()
                if conversation.project_id == project_id
                and conversation.deleted_at is None
                and (activity_type is None or conversation.activity_type == activity_type)
            ),
            key=lambda conversation: conversation.updated_at,
            reverse=True,
        )

    def get_conversation_record(self, conversation_id: str) -> Conversation | None:
        conversation = self._conversations.get(conversation_id)
        return conversation if conversation is not None and conversation.deleted_at is None else None

    def rename_conversation_record(self, conversation_id: str,
                                   title: str) -> Conversation | None:
        conversation = self.get_conversation_record(conversation_id)
        if conversation is None:
            return None
        from datetime import datetime, timezone
        conversation.title = title
        conversation.updated_at = datetime.now(timezone.utc)
        return conversation

    def delete_conversation_record(self, conversation_id: str) -> bool:
        conversation = self.get_conversation_record(conversation_id)
        if conversation is None:
            return False
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        conversation.deleted_at = now
        conversation.updated_at = now
        return True

    def save_message(self, message: Message) -> None:
        self._messages[message.conversation_id].append(message)

    def messages_for_conversation(self, conversation_id: str) -> list[Message]:
        return list(self._messages.get(conversation_id, []))

    def save_run_record(self, run: Run) -> None:
        self._runs[run.run_id] = run

    def get_run_record(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def find_run_record(self, conversation_id: str, idempotency_key: str) -> Run | None:
        return next((
            run for run in self._runs.values()
            if run.conversation_id == conversation_id and run.idempotency_key == idempotency_key
        ), None)

    def save_run_event_records(self, events: list[RunEvent]) -> None:
        for event in events:
            self._run_events[event.run_id].append(event)

    def run_event_records(self, run_id: str, *, after_sequence: int = -1) -> list[RunEvent]:
        return [
            event for event in self._run_events.get(run_id, [])
            if event.sequence > after_sequence
        ]

    def add_book(self, book: Book) -> None:
        self.books[book.book_id] = book

    def get_source(self, source_id: str) -> Book | None:
        return self.books.get(source_id)

    def find_source_by_hash(self, learner_id: str, source_hash: str) -> Book | None:
        return next((
            source for source in self.books.values()
            if source.owner_user_id == learner_id and source.source_hash == source_hash
        ), None)

    def update_source_metadata(self, source_id: str, *, parser_version: str | None = None,
                               page_count: int | None = None,
                               section_count: int | None = None,
                               outline: list[dict] | None = None) -> None:
        source = self.books.get(source_id)
        if source is None:
            raise ScopeError(f"unknown source {source_id}")
        if parser_version is not None:
            source.parser_version = parser_version
        if page_count is not None:
            source.page_count = page_count
        if section_count is not None:
            source.section_count = section_count
        if outline is not None:
            source.outline = list(outline)

    def link_book(self, pb: ProjectBook) -> None:
        # Enforce: one PRIMARY per project.
        if pb.role.value == "PRIMARY":
            existing = [x for x in self.project_books[pb.project_id] if x.role.value == "PRIMARY"]
            if existing:
                raise ScopeError(f"project {pb.project_id} already has a PRIMARY book")
        # Enforce: unique (project_id, book_id).
        if any(x.book_id == pb.book_id for x in self.project_books[pb.project_id]):
            raise ScopeError(f"book {pb.book_id} already linked to project {pb.project_id}")
        self.project_books[pb.project_id].append(pb)

    def allowed_book_ids(self, project_id: str, *, only_enabled: bool = True) -> set[str]:
        ids = set()
        for pb in self.project_books[project_id]:
            if only_enabled and not pb.enabled_for_retrieval:
                continue
            ids.add(pb.book_id)
        return ids

    def book_accessible_by(self, book_id: str, learner_id: str) -> bool:
        """A book is accessible if the learner owns it or has a project linking it."""
        book = self.books.get(book_id)
        if book is None:
            return False
        if book.owner_user_id == learner_id:
            return True
        # any project of this learner linking the book
        for pid, pbs in self.project_books.items():
            proj = self.projects.get(pid)
            if proj and proj.learner_id == learner_id and any(x.book_id == book_id for x in pbs):
                return True
        return False

    # --- concepts ----------------------------------------------------------

    def add_concept(self, concept: Concept) -> None:
        self.concepts[concept.book_id].append(concept)

    def concepts_for_book(self, book_id: str) -> list[Concept]:
        return list(self.concepts.get(book_id, []))

    def replace_book_graph(
        self, book_id: str, concepts: list[Concept], relations: list[ConceptRelation]
    ) -> None:
        """Atomically replace one book's semantic graph.

        Relation rows do not carry ``book_id`` in the domain model, so scope is
        derived from both the previous and replacement concept ids.
        """
        old_ids = {c.concept_id for c in self.concepts.get(book_id, [])}
        new_ids = {c.concept_id for c in concepts}
        scoped_ids = old_ids | new_ids
        self.concepts[book_id] = list(concepts)
        self.relations = [
            r for r in self.relations if r.source_concept_id not in scoped_ids
        ] + list(relations)

    def relations_for_book(self, book_id: str) -> list[ConceptRelation]:
        ids = {c.concept_id for c in self.concepts.get(book_id, [])}
        return [r for r in self.relations if r.source_concept_id in ids]

    def concept_in_project_scope(self, concept_id: str, project_id: str) -> bool:
        allowed = self.allowed_book_ids(project_id)
        for bid in allowed:
            if any(c.concept_id == concept_id for c in self.concepts.get(bid, [])):
                return True
        return False

    # --- learner state -----------------------------------------------------

    def get_state(self, project_id: str, concept_id: str) -> LearnerConceptState:
        key = (project_id, concept_id)
        if key not in self.states:
            self.states[key] = LearnerConceptState(project_id=project_id, concept_id=concept_id)
        return self.states[key]

    def save_state(self, state: LearnerConceptState) -> None:
        self.states[(state.project_id, state.concept_id)] = state

    # --- evidence ----------------------------------------------------------

    def append_evidence(self, evidence: Evidence) -> bool:
        """Append if event_key is new; return True if written, False if duplicate."""
        if evidence.event_key in self._evidence_by_key:
            return False  # idempotent: replay does nothing
        self.evidence.append(evidence)
        self._evidence_by_key[evidence.event_key] = evidence
        return True

    def evidence_for(self, project_id: str, concept_id: str) -> list[Evidence]:
        return [e for e in self.evidence if e.project_id == project_id and e.concept_id == concept_id]

    def evidence_for_misconception(self, project_id: str, bug_id: str) -> list[Evidence]:
        return [
            e for e in self.evidence
            if e.project_id == project_id and any(s.bug_id == bug_id for s in e.misconception_signals)
            or (e.evidence_type.value == "CHANGED_TASK" and self._ct_belongs_to_bug(e, project_id, bug_id))
        ]

    def _ct_belongs_to_bug(self, e: Evidence, project_id: str, bug_id: str) -> bool:
        # changed-task evidence is linked via discriminated_bug_ids or signals.
        return bug_id in (e.discriminated_bug_ids or [])

    def evidence_for_project(self, project_id: str) -> list[Evidence]:
        return [e for e in self.evidence if e.project_id == project_id]

    # --- trusted tasks / submissions (M4) ----------------------------------

    def save_trusted_task(self, task_data: dict) -> None:
        self._trusted_tasks[task_data["task_id"]] = dict(task_data)

    def get_trusted_task(self, task_id: str) -> dict | None:
        t = self._trusted_tasks.get(task_id)
        return dict(t) if t is not None else None

    def pending_task_for_conversation(self, conversation_id: str) -> dict | None:
        for t in self._trusted_tasks.values():
            if t.get("status") == "PENDING" and t.get("conversation_id") == conversation_id:
                return dict(t)
        return None

    def pending_task_for_project(self, project_id: str) -> dict | None:
        for t in self._trusted_tasks.values():
            if t.get("status") == "PENDING" and t.get("project_id") == project_id:
                return dict(t)
        return None

    def update_task_status(self, task_id: str, status: str, *, last_submission_id: str | None = None) -> None:
        t = self._trusted_tasks.get(task_id)
        if t is None:
            return
        t["status"] = status
        if last_submission_id is not None:
            t["last_submission_id"] = last_submission_id

    def increment_task_hints(self, task_id: str) -> int:
        t = self._trusted_tasks.get(task_id)
        if t is None:
            return 0
        t["hints_issued"] = int(t.get("hints_issued", 0)) + 1
        return int(t["hints_issued"])

    def save_submission(self, submission_data: dict) -> bool:
        idem = (submission_data.get("task_id", ""), submission_data.get("idempotency_key", ""))
        if idem in self._submissions_by_idem:
            return False  # idempotent: same (task_id, idempotency_key) already stored
        self._submissions[submission_data["submission_id"]] = dict(submission_data)
        self._submissions_by_idem[idem] = self._submissions[submission_data["submission_id"]]
        return True

    def get_submission_by_idem(self, task_id: str, idempotency_key: str) -> dict | None:
        s = self._submissions_by_idem.get((task_id, idempotency_key))
        return dict(s) if s is not None else None

    # --- misconceptions ----------------------------------------------------

    def get_misconception(self, project_id: str, bug_id: str) -> MisconceptionHypothesis | None:
        return self.misconceptions.get((project_id, bug_id))

    def upsert_misconception(self, mis: MisconceptionHypothesis) -> None:
        self.misconceptions[(mis.project_id, mis.bug_id)] = mis

    def all_misconceptions(self, project_id: str) -> list[MisconceptionHypothesis]:
        return [m for (pid, _), m in self.misconceptions.items() if pid == project_id]

    # --- transitions -------------------------------------------------------

    def record_transition(self, t: StateTransition) -> None:
        self.transitions.append(t)

    def transitions_for(self, project_id: str, *, entity_type: str | None = None,
                        entity_id: str | None = None) -> list[StateTransition]:
        return [
            transition for transition in self.transitions
            if transition.project_id == project_id
            and (entity_type is None or transition.entity_type == entity_type)
            and (entity_id is None or transition.entity_id == entity_id)
        ]

    # --- expiry idempotency (LEARNING_MODEL §6) ---------------------------

    def record_expiry_key(self, key: str) -> None:
        self._expiry_keys.add(key)

    def expiry_keys(self) -> set[str]:
        return self._expiry_keys

    # --- retrieval artifacts -----------------------------------------------

    def add_chunks(self, book_id: str, chunks: list["DocumentChunk"]) -> None:
        existing_ids = {c.chunk_id for c in self.chunks[book_id]}
        for c in chunks:
            if c.chunk_id not in existing_ids:
                self.chunks[book_id].append(c)
                existing_ids.add(c.chunk_id)

    def chunks_for_book(self, book_id: str) -> list["DocumentChunk"]:
        return list(self.chunks.get(book_id, []))

    def chunks_for_project(self, project_id: str, *, only_enabled: bool = True) -> list["DocumentChunk"]:
        out: list["DocumentChunk"] = []
        for bid in self.allowed_book_ids(project_id, only_enabled=only_enabled):
            out.extend(self.chunks_for_book(bid))
        return out

    def chunk_by_id(self, chunk_id: str) -> "DocumentChunk | None":
        for chunks in self.chunks.values():
            for c in chunks:
                if c.chunk_id == chunk_id:
                    return c
        return None

    def set_retriever(self, project_id: str, retriever: "HybridRetriever") -> None:
        self._retrievers[project_id] = retriever

    def get_retriever(self, project_id: str) -> "HybridRetriever | None":
        return self._retrievers.get(project_id)

    # --- snapshot ----------------------------------------------------------

    def snapshot(self) -> "InMemoryRepository":
        return copy.deepcopy(self)
