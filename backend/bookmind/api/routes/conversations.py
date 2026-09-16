"""Conversation routes — persistent messages and asynchronous QA runs.

Sending a message durably creates the user message and queued run, returns
HTTP 202 immediately, and lets the browser follow retrieval/model progress via
the run SSE endpoint.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ...domain.models import User
from ...storage.protocols import Repository
from ..dependencies import (
    get_conversation_worker, get_current_user, get_orchestrator, get_repo, get_run_service,
)
from ..errors import AppError
from ...services.conversation_orchestrator import ConversationOrchestrator
from ...services.run_service import RunService
from ...services.conversation_worker import ConversationWorker

router = APIRouter(prefix="/api", tags=["conversations"])


class SendMessageBody(BaseModel):
    content: str
    idempotency_key: str = ""
    source_id: str = ""
    source_page: int | None = Field(default=None, ge=1)
    source_scope: Literal["CURRENT_PAGE", "CURRENT_SOURCE", "ALL_SOURCES"] = "ALL_SOURCES"
    selection_text: str = ""
    record_question_signal: bool = True

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message is empty")
        if len(value) > 20_000:
            raise ValueError("message is too long")
        return value

    @field_validator("selection_text")
    @classmethod
    def limit_selection(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 5_000:
            raise ValueError("selection is too long")
        return value


@router.get("/agent-workflow")
def agent_workflow(
    orch: ConversationOrchestrator = Depends(get_orchestrator),
) -> dict:
    """Return the live LangGraph topology for inspection and competition QA."""
    return {
        "runtime": "langgraph",
        "workflow": "bookmind_learning_conversation",
        "mermaid": orch.workflow_mermaid(),
    }


@router.get("/projects/{project_id}/conversations")
def list_conversations(
    project_id: str,
    activity_type: Literal["LEARN", "REVIEW", "ASSESSMENT"] | None = None,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> list[dict]:
    repo.assert_project_owned_by(project_id, user.user_id)
    return [
        {"conversation_id": c.conversation_id, "title": c.title,
         "activity_type": c.activity_type,
         "created_at": c.created_at.isoformat(), "updated_at": c.updated_at.isoformat()}
        for c in runs.list_conversations(project_id, activity_type)
    ]


@router.post("/projects/{project_id}/conversations")
def create_conversation(
    project_id: str,
    activity_type: Literal["LEARN", "REVIEW", "ASSESSMENT"] = "LEARN",
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> dict:
    repo.assert_project_owned_by(project_id, user.user_id)
    conv = runs.create_conversation(project_id=project_id, activity_type=activity_type)
    return {"conversation_id": conv.conversation_id, "title": conv.title,
            "activity_type": conv.activity_type,
            "created_at": conv.created_at.isoformat(),
            "updated_at": conv.updated_at.isoformat()}


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> dict:
    conv = runs.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    messages = runs.messages_for(conversation_id)
    return {
        "conversation_id": conv.conversation_id,
        "project_id": conv.project_id,
        "activity_type": conv.activity_type,
        "title": conv.title,
        "messages": [
            {"message_id": m.message_id, "role": m.role,
             "content_blocks": [b.model_dump() for b in m.content_blocks],
             "run_id": m.run_id, "created_at": m.created_at.isoformat()}
            for m in messages
        ],
    }


class RenameBody(BaseModel):
    title: str


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str,
    body: RenameBody,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> dict:
    """Rename a conversation (PRODUCTIZATION §5.12). Does not touch Evidence."""
    conv = runs.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    updated = runs.rename_conversation(conversation_id, body.title)
    if updated is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"conversation_id": updated.conversation_id, "title": updated.title}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> dict:
    """Soft-delete a conversation. Evidence and project learning state are
    preserved (PRODUCTIZATION §5.12)."""
    conv = runs.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    runs.delete_conversation(conversation_id)
    return {"conversation_id": conversation_id, "deleted": True}


@router.post("/conversations/{conversation_id}/messages", status_code=202)
def send_message(
    conversation_id: str,
    body: SendMessageBody,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    orch: ConversationOrchestrator = Depends(get_orchestrator),
    worker: ConversationWorker = Depends(get_conversation_worker),
) -> dict:
    """Persist the user message, queue an async run, and return HTTP 202."""
    conv = runs.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    proj = repo.assert_project_owned_by(conv.project_id, user.user_id)
    if body.source_id and body.source_id not in repo.allowed_book_ids(conv.project_id):
        raise AppError("SOURCE_NOT_IN_SCOPE", "这份资料不在当前学习空间中", status_code=404)

    # P1-12: idempotency on (conversation_id, idempotency_key). A retried send
    # with the same key replays the prior run instead of creating a new run and
    # duplicate messages. The frontend generates a stable per-send key.
    idem = body.idempotency_key or f"msg_{uuid.uuid4().hex[:12]}"
    existing = runs.find_run_by_idempotency(conversation_id, idem)
    if existing is not None:
        return {
            "message_id": existing.message_id,
            "run_id": existing.run_id,
            "assistant_message_id": None,
            "status": existing.status,
            "replay": True,
        }

    # Queue the turn in the independent chat pool and return immediately.
    run, user_message, assistant_message_id = worker.submit(
        orchestrator=orch, conversation=conv, user_text=body.content,
        learner_id=user.user_id, project_id=conv.project_id,
        idempotency_key=idem, history=runs.messages_for(conversation_id),
        source_context={
            "source_id": body.source_id,
            "page": body.source_page,
            "scope": body.source_scope,
            "selection_text": body.selection_text,
            "record_question_signal": body.record_question_signal,
        },
    )
    return {
        "message_id": user_message.message_id,
        "run_id": run.run_id,
        "assistant_message_id": assistant_message_id,
        "status": run.status,
    }
