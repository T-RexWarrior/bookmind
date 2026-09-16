"""Small, auditable memory helpers for learner-facing continuity.

The memory store is deliberately narrow: it holds declarations and contextual
anchors, not model-inferred personality claims.  It is project-scoped and
never bypasses the evidence gate used for L1--L4 mastery.
"""

from __future__ import annotations

import hashlib

from ..domain.enums import MemoryKind
from ..domain.models import LearningMemory, utcnow
from ..storage.protocols import Repository


def manual_memory_id(project_id: str, concept_id: str) -> str:
    digest = hashlib.sha256(f"{project_id}|{concept_id}|manual-learned".encode("utf-8")).hexdigest()[:24]
    return f"mem_manual_{digest}"


def set_manual_learned(repo: Repository, *, project_id: str, concept_id: str, learned: bool) -> bool:
    """Set or clear a learner declaration without creating mastery evidence."""
    memory_id = manual_memory_id(project_id, concept_id)
    if not learned:
        return repo.delete_memory(memory_id)
    now = utcnow()
    repo.save_memory(LearningMemory(
        memory_id=memory_id, project_id=project_id,
        kind=MemoryKind.MANUAL_LEARNED, concept_id=concept_id,
        content="learner_marked_learned",
        metadata={"display_label": "已学（待验证）"},
        created_at=now, updated_at=now,
    ))
    return True


def manually_learned_ids(repo: Repository, project_id: str) -> set[str]:
    return {
        item.concept_id
        for item in repo.memories_for_project(project_id, kind=MemoryKind.MANUAL_LEARNED.value)
        if item.concept_id
    }


def remember_question_context(
    repo: Repository, *, project_id: str, concept_id: str, conversation_id: str,
    question: str, run_id: str, confidence: float,
) -> None:
    """Store a compact topic anchor for safe later-context construction."""
    digest = hashlib.sha256(f"{project_id}|{run_id}|{concept_id}|question".encode("utf-8")).hexdigest()[:24]
    now = utcnow()
    repo.save_memory(LearningMemory(
        memory_id=f"mem_question_{digest}", project_id=project_id,
        kind=MemoryKind.QUESTION_CONTEXT, concept_id=concept_id,
        conversation_id=conversation_id, content=question[:800],
        metadata={"confidence": round(confidence, 3), "run_id": run_id},
        created_at=now, updated_at=now,
    ))


def remember_task_followup(
    repo: Repository, *, project_id: str, conversation_id: str, task_id: str,
    question: str,
) -> None:
    digest = hashlib.sha256(f"{project_id}|{task_id}|{question}".encode("utf-8")).hexdigest()[:24]
    now = utcnow()
    repo.save_memory(LearningMemory(
        memory_id=f"mem_followup_{digest}", project_id=project_id,
        kind=MemoryKind.TASK_FOLLOWUP, conversation_id=conversation_id,
        content=question[:1200], metadata={"task_id": task_id},
        created_at=now, updated_at=now,
    ))

