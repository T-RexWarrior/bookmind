"""Service layer — orchestrates use cases across agents, engine and retrieval.

ARCHITECTURE §2 dependency rule: ``API → Services → Agents / Engine / Retrieval
→ Repository / VectorStore``. Services manage transactions and scope; they are
the only layer that wires an Agent + Engine + Retrieval call together.
"""

from __future__ import annotations

from .book_mapping import BookMappingService, MappingReport
from .book_qa import AskResult, BookQAService
from .context_builder import BuiltContext, ContextBuilder, ContextRequest
from .learner_state_view import LearnerStateView, project_state_views
from .misconception_view import MisconceptionTrace, build_trace, project_traces
from .recovery import RecoveryPlan, RecoveryService, project_recovery_plan
from .remediation import ChangedTaskResult, RemediationPlan, RemediationService

__all__ = [
    "AskResult", "BookQAService", "BuiltContext", "ContextBuilder", "ContextRequest",
    "BookMappingService", "MappingReport",
    "LearnerStateView", "project_state_views",
    "MisconceptionTrace", "build_trace", "project_traces",
    "RecoveryService", "RecoveryPlan", "project_recovery_plan",
    "RemediationService", "RemediationPlan", "ChangedTaskResult",
]
