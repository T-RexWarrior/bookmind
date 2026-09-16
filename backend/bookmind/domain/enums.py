"""Enumerated domain constants.

These are the project-wide canonical names. Per LEARNING_MODEL.md, every
status, action and event uses these exact names; no other module may coin a
synonym.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """``str`` + ``Enum`` with stable ``value``-based membership.

    We hand-roll this instead of ``enum.StrEnum`` so the codebase stays
    compatible across Python 3.10–3.13 and so ``json.dumps`` keeps working
    without a custom encoder (members *are* plain strings).
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


# --- Identity / scoping ---------------------------------------------------

class BookRole(StrEnum):
    PRIMARY = "PRIMARY"
    REFERENCE = "REFERENCE"


# --- Book Graph -----------------------------------------------------------

class ConceptSource(StrEnum):
    GOLD = "GOLD"
    LLM_PROPOSED = "LLM_PROPOSED"
    MANUAL_CONFIRMED = "MANUAL_CONFIRMED"


class Difficulty(StrEnum):
    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


class RelationType(StrEnum):
    PREREQUISITE = "PREREQUISITE"
    RELATED = "RELATED"


# --- Exposure -------------------------------------------------------------

class ExposureState(StrEnum):
    NONE = "NONE"
    SEEN = "SEEN"
    COMPLETED = "COMPLETED"


# --- Mastery L0–L4 --------------------------------------------------------

class Level(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


# Ordered levels for "higher than" comparisons.
LEVEL_ORDER: list[Level] = [Level.L0, Level.L1, Level.L2, Level.L3, Level.L4]


def level_index(level: Level) -> int:
    """Return the 0–4 position of a level."""
    return LEVEL_ORDER.index(level)


def is_higher(a: Level, b: Level) -> bool:
    return level_index(a) > level_index(b)


class LevelStatus(StrEnum):
    """Per-level verification status (LEARNING_MODEL §3, §5)."""
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    UNSTABLE = "UNSTABLE"
    EXPIRED = "EXPIRED"


# Derived display-only status, not written back to ``level_status``.
class DerivedEffectiveStatus(StrEnum):
    VERIFIED = "VERIFIED"
    BLOCKED_BY_LOWER_LEVEL = "BLOCKED_BY_LOWER_LEVEL"
    UNVERIFIED = "UNVERIFIED"
    EXPIRED = "EXPIRED"
    UNSTABLE = "UNSTABLE"


# --- Evidence -------------------------------------------------------------

class EvidenceType(StrEnum):
    READ = "READ"
    QUESTION = "QUESTION"
    EXPLANATION = "EXPLANATION"
    VERIFY = "VERIFY"
    PROBE = "PROBE"
    CHANGED_TASK = "CHANGED_TASK"
    CORRECTION = "CORRECTION"


class EvidenceResult(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"


class HintLevel(int, Enum):
    """0–3 hint exposure recorded by the trusted InteractionContext."""
    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3


# --- Misconception --------------------------------------------------------

class MisconceptionStatus(StrEnum):
    SUSPECTED = "SUSPECTED"
    LIKELY = "LIKELY"
    CONFIRMED = "CONFIRMED"
    DISMISSED = "DISMISSED"
    REMEDIATING = "REMEDIATING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    RELAPSED = "RELAPSED"


class ConfidenceBand(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SignalDirection(StrEnum):
    FOR = "FOR"
    AGAINST = "AGAINST"


class SignalStrength(StrEnum):
    WEAK = "WEAK"
    MEDIUM = "MEDIUM"
    STRONG = "STRONG"


# --- Learning modes (LEARNING_MODEL §10) ----------------------------------

class ActivityMode(StrEnum):
    READING = "READING"
    REVIEW = "REVIEW"
    ASSESSMENT = "ASSESSMENT"


class InterventionPolicy(StrEnum):
    QUIET = "QUIET"
    PROACTIVE = "PROACTIVE"


class UIPreset(StrEnum):
    QUIET_READING = "Quiet Reading"
    DEEP_LEARNING = "Deep Learning"
    REVIEW = "Review"
    ASSESSMENT = "Assessment"


# --- Actions (LEARNING_MODEL §11) -----------------------------------------

class Action(StrEnum):
    ANSWER = "ANSWER"
    CONTINUE_READING = "CONTINUE_READING"
    VERIFY = "VERIFY"
    DIAGNOSE = "DIAGNOSE"
    LEARN_PREREQUISITE = "LEARN_PREREQUISITE"
    REVIEW = "REVIEW"
    REMEDIATE = "REMEDIATE"
    WAIT = "WAIT"


class AgentName(StrEnum):
    BOOK_MAPPER = "book_mapper"
    TUTOR = "tutor"
    DIAGNOSTICIAN = "diagnostician"
    ENGINE = "engine"


# --- AnswerJudgment -------------------------------------------------------

class JudgmentStatus(StrEnum):
    DECIDED = "DECIDED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


# --- Ingestion ------------------------------------------------------------

class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    FAILED = "FAILED"


# --- SSE / trace event names (ARCHITECTURE §6) ----------------------------

class EventType(StrEnum):
    RUN_STARTED = "run_started"
    MODE_SELECTED = "mode_selected"
    ACTION_SELECTED = "action_selected"
    AGENT_STARTED = "agent_started"
    AGENT_DELTA = "agent_delta"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    AGENT_COMPLETED = "agent_completed"
    CITATION_ATTACHED = "citation_attached"
    FALLBACK_USED = "fallback_used"
    EVIDENCE_CREATED = "evidence_created"
    STATE_UPDATED = "state_updated"
    REVIEW_SCHEDULED = "review_scheduled"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RETRIEVAL_COMPLETED = "retrieval_completed"
    SOURCE_LOCATIONS_READY = "source_locations_ready"
    ANSWER_DELTA = "answer_delta"
    ANSWER_COMPLETED = "answer_completed"
    ANSWER_UNAVAILABLE = "answer_unavailable"
