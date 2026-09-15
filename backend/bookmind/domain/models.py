"""Pydantic domain models — the shared contracts of the system.

These schemas are the "single source of truth" referenced by LEARNING_MODEL.md
and ARCHITECTURE.md. They are intentionally framework-agnostic (no ORM, no DB)
so the deterministic Engine can reason over pure data structures. Persistence
repositories translate to/from these models.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .enums import (
    ActivityMode,
    AgentName,
    BookRole,
    ConfidenceBand,
    Difficulty,
    DerivedEffectiveStatus,
    EvidenceResult,
    EvidenceType,
    ExposureState,
    HintLevel,
    InterventionPolicy,
    JudgmentStatus,
    Level,
    LevelStatus,
    MisconceptionStatus,
    RelationType,
    SignalDirection,
    SignalStrength,
    UIPreset,
)
from .source_ref import SourceRef


def utcnow() -> datetime:
    """UTC now, timezone-aware. Centralised so tests can monkeypatch it."""
    return datetime.now(timezone.utc)


# --- Identity / scoping ---------------------------------------------------

class User(BaseModel):
    user_id: str
    display_name: str = ""


class LearningProject(BaseModel):
    project_id: str
    learner_id: str
    name: str
    # A learning space is organised around the material and the learner's
    # intention, not around an exam.  All planning fields are optional so a
    # learner can simply open a source and start reading.
    goal: str = ""
    learning_scope: str = ""
    deadline: str = ""
    current_plan: str = ""
    last_source_id: str = ""
    last_source_page: int = Field(default=1, ge=1)
    default_mode: UIPreset = UIPreset.QUIET_READING
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_activity_at: datetime = Field(default_factory=utcnow)
    archived_at: datetime | None = None


class LearningSource(BaseModel):
    """One material inside a learning space.

    ``book_id`` remains the persisted compatibility key for the existing
    learning engine.  New product APIs expose it as ``source_id`` and all new
    code should prefer that name.  Keeping the storage key avoids a destructive
    migration of users' existing evidence and concept graphs.
    """

    book_id: str
    owner_user_id: str
    source_hash: str
    title: str
    edition: str = ""
    parser_version: str = ""
    source_type: str = "PDF"
    original_filename: str = ""
    page_count: int = 0
    section_count: int = 0
    outline: list[dict] = Field(default_factory=list)

    @property
    def source_id(self) -> str:
        return self.book_id


class ProjectSource(BaseModel):
    project_id: str
    book_id: str
    role: BookRole
    enabled_for_retrieval: bool = True
    added_at: datetime = Field(default_factory=utcnow)

    @property
    def source_id(self) -> str:
        return self.book_id


# Backward-compatible names used by the deterministic engine and old API.
# Product-facing code uses LearningSource / ProjectSource.
Book = LearningSource
ProjectBook = ProjectSource


# --- Book Graph -----------------------------------------------------------

class Concept(BaseModel):
    concept_id: str
    book_id: str
    name: str
    description: str = ""
    chapter: str = ""
    section: str = ""
    source_refs: list[SourceRef] = Field(default_factory=list)
    importance: float = Field(default=0.0, ge=0.0, le=1.0)
    difficulty: Difficulty = Difficulty.MEDIUM
    source: str = "LLM_PROPOSED"  # ConceptSource value
    # Adjacency stored as concept ids; relation type disambiguates.
    prerequisites: list[str] = Field(default_factory=list)
    related_concepts: list[str] = Field(default_factory=list)
    goal_relevance: float = Field(default=0.0, ge=0.0, le=1.0)


class ConceptRelation(BaseModel):
    source_concept_id: str
    target_concept_id: str
    relation: RelationType
    source: str = "LLM_PROPOSED"
    rationale: str = ""


# --- Learner concept state (LEARNING_MODEL §3) ----------------------------

class LevelRecord(BaseModel):
    """Per-level (L1–L4) verification record."""

    status: LevelStatus = LevelStatus.UNVERIFIED
    verified_at: datetime | None = None
    stability_days: float = 0.0
    retrievability: float = 0.0
    review_due_at: datetime | None = None

    def is_verified(self) -> bool:
        return self.status == LevelStatus.VERIFIED


class LearnerConceptState(BaseModel):
    """The full cognitive state for one concept in one project."""

    project_id: str
    concept_id: str

    exposure_state: ExposureState = ExposureState.NONE
    read_progress: float = Field(default=0.0, ge=0.0, le=1.0)

    highest_ever_level: Level = Level.L0
    current_verified_level: Level = Level.L0

    levels: dict[str, LevelRecord] = Field(
        default_factory=lambda: {lvl.value: LevelRecord() for lvl in [Level.L1, Level.L2, Level.L3, Level.L4]}
    )
    goal_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    version: int = 0

    def level_record(self, level: Level) -> LevelRecord:
        return self.levels[level.value]

    def set_level_record(self, level: Level, record: LevelRecord) -> None:
        self.levels[level.value] = record

    def bump_version(self) -> None:
        self.version += 1


# --- Evidence Ledger (LEARNING_MODEL §4) ----------------------------------

class MisconceptionSignal(BaseModel):
    bug_id: str
    direction: SignalDirection
    strength: SignalStrength
    reason: str = ""


class Evidence(BaseModel):
    """An append-only ledger entry. ``event_key`` makes submission idempotent."""

    evidence_id: str
    event_key: str  # unique; derived from project+task+version+submission
    project_id: str
    concept_id: str
    source_book_id: str
    source_refs: list[SourceRef] = Field(default_factory=list)
    source_chunk_ids: list[str] = Field(default_factory=list)
    evidence_type: EvidenceType
    required_level: Level = Level.L0
    result: EvidenceResult | None = None
    independent: bool = False
    hint_level: HintLevel = HintLevel.NONE
    task_id: str = ""
    task_version: int = 0
    occurred_at: datetime = Field(default_factory=utcnow)
    source_session: str = ""
    content_summary: str = ""
    misconception_signals: list[MisconceptionSignal] = Field(default_factory=list)
    correction_of_id: str | None = None
    # For PROBE / CHANGED_TASK evidence: which hypothesis it discriminated.
    discriminated_bug_ids: list[str] = Field(default_factory=list)
    scenario_fingerprint: str | None = None
    high_discrimination: bool = False
    scoring_type: str = ""  # which fixed-evidence-score bucket this counted as

    model_config = {"frozen": True}


# --- Misconception (LEARNING_MODEL §8) ------------------------------------

class MisconceptionHypothesis(BaseModel):
    project_id: str
    bug_id: str
    related_concepts: list[str] = Field(default_factory=list)
    hypothesis_group: str | None = None
    evidence_score: int = 0
    confidence_band: ConfidenceBand = ConfidenceBand.LOW
    status: MisconceptionStatus = MisconceptionStatus.SUSPECTED
    evidence_ids: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    remediation_version: int | None = None
    changed_task_pass_count: int = 0
    changed_task_pass_fingerprints: list[str] = Field(default_factory=list)
    hypothesis_cycle: int = 0
    updated_at: datetime = Field(default_factory=utcnow)


# --- Trusted task / interaction contexts (LEARNING_MODEL §4) --------------

class TrustedTaskContext(BaseModel):
    """Server-side, immutable task definition. Diagnostician must not override
    ``required_level`` / ``target_concept_ids`` / ``rubric``."""

    task_id: str
    task_version: int
    target_concept_ids: list[str]
    evidence_for_levels: list[Level]
    rubric: list[str]
    allowed_resources: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    scenario_fingerprint: str | None = None
    is_probe: bool = False
    discriminated_bug_ids: list[str] = Field(default_factory=list)
    is_changed_task: bool = False
    remediation_stage: int = 0  # 1 = near transfer, 2 = far transfer

    @model_validator(mode="after")
    def _check_levels(self) -> "TrustedTaskContext":
        if not self.evidence_for_levels:
            raise ValueError("TrustedTaskContext must declare evidence_for_levels")
        return self


class InteractionContext(BaseModel):
    """Server-side facts about the interaction — what help the learner saw."""

    activity_mode: ActivityMode
    intervention_policy: InterventionPolicy
    ui_preset: UIPreset
    hints_issued: int = 0
    tools_exposed: list[str] = Field(default_factory=list)
    answer_started_at: datetime | None = None
    answer_submitted_at: datetime | None = None


# --- AnswerJudgment (LEARNING_MODEL §7) -----------------------------------

class CriterionResult(BaseModel):
    criterion_id: str
    satisfied: bool
    note: str = ""


class TargetConceptResult(BaseModel):
    concept_id: str
    result: EvidenceResult


class AnswerJudgment(BaseModel):
    """Structured output of the Diagnostician. Language understanding only —
    the Engine applies the fixed rules."""

    judgment_status: JudgmentStatus
    result: EvidenceResult | None = None
    criterion_results: list[CriterionResult] = Field(default_factory=list)
    target_concept_results: list[TargetConceptResult] = Field(default_factory=list)
    observed_related_concepts: list[str] = Field(default_factory=list)
    misconception_signals: list[MisconceptionSignal] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="after")
    def _consistent_result(self) -> "AnswerJudgment":
        if self.judgment_status == JudgmentStatus.DECIDED and self.result is None:
            raise ValueError("DECIDED judgment requires a result")
        if self.judgment_status == JudgmentStatus.NEEDS_REVIEW and self.result is not None:
            raise ValueError("NEEDS_REVIEW judgment must not set a result")
        return self


# --- Review policy (versioned) -------------------------------------------

class ReviewPolicy(BaseModel):
    """FSRS-inspired scheduling params. NOT a full FSRS implementation.

    Per LEARNING_MODEL §6: params are versioned; changing them bumps
    ``review_policy_version`` and requires re-running fixed-timepoint tests.
    """

    review_policy_version: int = 1
    initial_stability_days: dict[str, float] = Field(
        default_factory=lambda: {Level.L1.value: 2.0, Level.L2.value: 4.0, Level.L3.value: 7.0, Level.L4.value: 14.0}
    )
    pass_multiplier: float = 1.8
    partial_reschedule_days: float = 1.0
    fail_reschedule_factor: float = 0.25
    expiry_retrievability_threshold: float = 0.9
    retrievability_scale_days: float = 9.0  # the ``9`` in R_k(t) = (1 + t/(9S))^-1


# --- Decision / trace (LEARNING_MODEL §13, ARCHITECTURE §6) ---------------

class DecisionCandidate(BaseModel):
    concept_id: str
    action: str  # Action value
    sort_keys: dict[str, float] = Field(default_factory=dict)
    rule_index: int


class DecisionTrace(BaseModel):
    project_id: str
    activity_mode: ActivityMode
    intervention_policy: InterventionPolicy
    ui_preset: UIPreset
    checked_rules: list[int] = Field(default_factory=list)
    selected_rule: int
    candidates: list[DecisionCandidate] = Field(default_factory=list)
    selected_action: str  # Action value
    selected_concept_id: str | None = None
    reason: str = ""
    related_evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class StateTransition(BaseModel):
    """Unified record of mastery & misconception changes (ARCHITECTURE §9)."""

    transition_id: str
    entity_type: Literal["mastery", "misconception", "review", "exposure"]
    entity_id: str
    project_id: str
    old_state: str
    new_state: str
    triggering_evidence_id: str | None = None
    rule_version: str
    created_at: datetime = Field(default_factory=utcnow)


# --- Phase 5: Task pipeline & misconception closure (ARCHITECTURE §3.5) ----

class TaskDraft(BaseModel):
    """A transient task proposal from Tutor/Diagnostician *before* it passes the
    shared Task Validator. The Validator converts a passing draft into an
    immutable :class:`TrustedTaskContext`.

    ``required_level`` / ``hint_level`` / ``independent`` are deliberately absent:
    those come from the trusted server context, never from the Agent.
    """

    task_id: str
    task_version: int = 1
    target_concept_ids: list[str]
    evidence_for_levels: list[Level]
    rubric: list[str]
    prompt_text: str = ""
    expected_answer: str = ""
    distractors: list[str] = Field(default_factory=list)
    is_probe: bool = False
    is_changed_task: bool = False
    discriminated_bug_ids: list[str] = Field(default_factory=list)
    scenario_fingerprint: str | None = None
    remediation_stage: int = 0  # 1 = near transfer, 2 = far transfer
    source_refs: list[SourceRef] = Field(default_factory=list)
    # Provenance is intentionally presentation-only.  It travels in the
    # persisted task-card message, but does not affect validation or grading.
    generation_mode: Literal["llm", "offline_fallback", "curated", "template"] = "offline_fallback"
    generation_notice: str = ""

    @model_validator(mode="after")
    def _check_levels(self) -> "TaskDraft":
        if not self.evidence_for_levels:
            raise ValueError("TaskDraft must declare evidence_for_levels")
        return self


class CheckResult(BaseModel):
    """Outcome of one Task Validator stage."""

    name: str  # schema / grounding / level / answerability / probe / dedup / execution
    passed: bool
    skipped: bool = False  # True for LLM-aid stages that degrade to skip offline
    detail: str = ""


class ValidationReport(BaseModel):
    """Result of validating a :class:`TaskDraft` through the shared gate.

    On success, ``trusted`` carries the immutable task context that may produce
    Evidence; on failure it is None and ``blocked_reasons`` explains why.
    """

    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    trusted: "TrustedTaskContext | None" = None


class RemediationPlan(BaseModel):
    """Rendered remediation content for one confirmed bug (LEARNING_MODEL §9).

    The minimal intervention: a root-cause explanation, a positive example,
    a counterexample, and two changed tasks of different scenarios.
    """

    bug_id: str
    explanation_goal: str
    positive_example: str
    counterexample: str
    rubric: list[str] = Field(default_factory=list)
    changed_task_stages: list[int] = Field(default_factory=lambda: [1, 2])


class EvidenceTraceItem(BaseModel):
    """One evidence entry as shown in the misconception trace view."""

    evidence_id: str
    evidence_type: str
    result: str | None
    scoring_type: str
    direction: str | None  # FOR / AGAINST / None
    strength: str | None
    task_id: str
    scenario_fingerprint: str | None
    high_discrimination: bool
    occurred_at: str


class MisconceptionTrace(BaseModel):
    """Read-side projection of one bug's full lifecycle (LEARNING_MODEL §13)."""

    bug_id: str
    status: str
    evidence_score: int
    confidence_band: str
    hypothesis_group: str | None
    changed_task_pass_count: int
    changed_task_pass_fingerprints: list[str] = Field(default_factory=list)
    hypothesis_cycle: int = 0
    remediation_version: int | None = None
    evidence_chain: list[EvidenceTraceItem] = Field(default_factory=list)
    transitions: list[dict] = Field(default_factory=list)


# --- Conversation / Message / Run (PRODUCTIZATION §6, M2) -----------------
#
# The conversation layer sits above the deterministic Engine. A Conversation
# belongs to a project; Messages belong to a conversation; a Run is the
# processing of one user message by the Orchestrator. State (Evidence, mastery)
# is written by the Engine, not by the conversation layer — conversations are
# the user-facing surface, the Engine remains the single state-write authority.

class ContentBlock(BaseModel):
    """One structured piece of a message (PRODUCTIZATION §7.4).

    Messages are a list of blocks, not a Markdown blob, so the frontend can
    render text / citations / task cards / state changes with explicit schemas.
    """
    type: Literal[
        "text", "citation", "context", "question_signal", "task",
        "state_change", "error", "status",
    ]
    # text block
    text: str = ""
    # citation block
    chunk_id: str = ""
    quote: str = ""
    page: str = ""
    book_id: str = ""
    label: str = ""  # human-readable citation label, e.g. "p.42 · 3.2 Polymorphism"
    # generic metadata
    data: dict = Field(default_factory=dict)


class Conversation(BaseModel):
    conversation_id: str
    project_id: str
    activity_type: Literal["LEARN", "REVIEW", "ASSESSMENT"] = "LEARN"
    title: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    deleted_at: datetime | None = None
    version: int = 1


class Message(BaseModel):
    message_id: str
    conversation_id: str
    role: Literal["user", "assistant", "system"]
    content_blocks: list[ContentBlock] = Field(default_factory=list)
    run_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Run(BaseModel):
    run_id: str
    conversation_id: str
    message_id: str
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"] = "QUEUED"
    intent: str = ""
    model: str = ""
    error: str = ""
    idempotency_key: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class RunEvent(BaseModel):
    """One SSE event in a run's lifecycle. ``event_type`` is an ``EventType``
    value; ``sequence`` is monotonic per run; ``payload`` carries the event
    body (e.g. agent_delta text, citation, state_delta)."""
    run_id: str
    sequence: int
    event_type: str  # EventType value
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


__all__ = [
    "User",
    "LearningProject",
    "LearningSource",
    "ProjectSource",
    "Book",
    "ProjectBook",
    "Concept",
    "ConceptRelation",
    "LevelRecord",
    "LearnerConceptState",
    "MisconceptionSignal",
    "Evidence",
    "MisconceptionHypothesis",
    "TrustedTaskContext",
    "InteractionContext",
    "CriterionResult",
    "TargetConceptResult",
    "AnswerJudgment",
    "ReviewPolicy",
    "DecisionCandidate",
    "DecisionTrace",
    "StateTransition",
    "DerivedEffectiveStatus",
    "TaskDraft",
    "CheckResult",
    "ValidationReport",
    "RemediationPlan",
    "EvidenceTraceItem",
    "MisconceptionTrace",
    "ContentBlock",
    "Conversation",
    "Message",
    "Run",
    "RunEvent",
]
