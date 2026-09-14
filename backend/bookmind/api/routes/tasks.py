"""Task routes — /api/tasks/{task_id} (PRODUCTIZATION §8.2, §M4).

The browser interacts with a server-owned task by ``task_id`` only. Answer
submission accepts ``answer_text`` + ``idempotency_key`` and nothing else — no
PASS/FAIL, no Concept ID, no rubric, no hint count, no misconception score. The
trusted task, the rubric, and the hint tally all live server-side; the
Diagnostician judges; the Learning Engine writes state.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from ...domain.enums import UIPreset
from ...domain.models import ContentBlock, Message, User
from ...storage.protocols import Repository
from ..dependencies import get_current_user, get_qa_service, get_repo, get_run_service, get_task_service
from ..errors import AppError
from ...services.run_service import RunService
from ...services.book_qa import BookQAService
from ...services.task_service import TaskService

router = APIRouter(prefix="/api", tags=["tasks"])


class AnswerBody(BaseModel):
    # The ONLY fields the browser may send for an answer.
    answer_text: str
    idempotency_key: str = ""

    @field_validator("answer_text")
    @classmethod
    def limit_answer(cls, value: str) -> str:
        if len(value) > 20_000:
            raise ValueError("answer is too long")
        return value.strip()


class CreateTaskBody(BaseModel):
    """Explicit UI command for starting a consolidation task.

    This is intentionally separate from chat messages: button clicks send
    structured intent and never rely on the model to infer control text.
    """

    mode: Literal["PRACTICE", "ASSESSMENT"]
    selection: Literal["RECOMMENDED", "QUESTIONED", "WEAK", "DUE", "UNVERIFIED", "ALL"] = "RECOMMENDED"
    concept_id: str = ""
    from_task_id: str = ""
    idempotency_key: str = ""


def _load_owned_task(repo: Repository, task_id: str, user: User) -> dict:
    """Load a task and verify it belongs to a project the user owns."""
    t = repo.get_trusted_task(task_id)
    if t is None:
        raise AppError("TASK_NOT_FOUND", "任务不存在", status_code=404)
    repo.assert_project_owned_by(t["project_id"], user.user_id)
    return t


@router.get("/projects/{project_id}/consolidation-candidates")
def consolidation_candidates(
    project_id: str,
    mode: Literal["PRACTICE", "ASSESSMENT"] = "PRACTICE",
    filter: Literal["RECOMMENDED", "QUESTIONED", "WEAK", "DUE", "UNVERIFIED", "ALL"] = "RECOMMENDED",
    source_id: str = "",
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    repo.assert_project_owned_by(project_id, user.user_id)
    if source_id and source_id not in repo.allowed_book_ids(project_id):
        raise AppError("SOURCE_NOT_IN_SCOPE", "这份资料不在当前学习空间中", status_code=404)
    return tasks.consolidation_candidates(
        project_id, mode=mode, filter=filter, source_id=source_id,
    )


@router.post("/conversations/{conversation_id}/tasks")
def create_task(
    conversation_id: str,
    body: CreateTaskBody,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    """Start a task from an explicit button action and persist its card."""
    conv = runs.get_conversation(conversation_id)
    if conv is None:
        raise AppError("CONVERSATION_NOT_FOUND", "对话不存在", status_code=404)
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    required_activity = "REVIEW" if body.mode == "PRACTICE" else "ASSESSMENT"
    if conv.activity_type != required_activity:
        raise AppError(
            "ACTIVITY_MISMATCH",
            "当前对话不属于所选巩固模式，请切换后重试",
            status_code=409,
            action="SWITCH_MODE",
        )
    selected_concept_id = body.concept_id
    if body.from_task_id:
        source_task = _load_owned_task(repo, body.from_task_id, user)
        if source_task["project_id"] != conv.project_id:
            raise AppError("TASK_NOT_IN_PROJECT", "原题不属于当前学习空间", status_code=404)
        selected_concept_id = next(iter(source_task.get("target_concept_ids") or []), "")
    if selected_concept_id and not repo.concept_in_project_scope(selected_concept_id, conv.project_id):
        raise AppError("CONCEPT_NOT_IN_SCOPE", "这个知识点不在当前学习空间中", status_code=404)

    pending = repo.pending_task_for_conversation(conversation_id)
    repo.update_project(
        conv.project_id,
        default_mode=UIPreset.REVIEW if body.mode == "PRACTICE" else UIPreset.ASSESSMENT,
    )
    payload = tasks.request_task(
        project_id=conv.project_id,
        learner_id=user.user_id,
        conversation_id=conversation_id,
        concept_id=selected_concept_id,
        selection=body.selection,
    )
    safe_fields = (
        "kind", "task_id", "prompt_text", "is_probe", "is_changed_task",
        "remediation_stage", "focus", "source_scope", "generation_reason",
        "hints_issued", "status", "text", "existing",
    )
    payload = {key: payload[key] for key in safe_fields if key in payload}
    if pending is not None:
        return {"task": payload, "message_id": None, "existing": True}

    if payload.get("task_id"):
        block = ContentBlock(type="task", data=payload)
    else:
        block = ContentBlock(type="error", text=payload.get("text", "暂时无法生成题目。"))
    message = Message(
        message_id=f"msg_{uuid.uuid4().hex[:12]}",
        conversation_id=conversation_id,
        role="assistant",
        content_blocks=[block],
    )
    runs.add_message(message)
    return {"task": payload, "message_id": message.message_id, "existing": False}


@router.post("/conversations/{conversation_id}/consolidation-summary")
def finish_consolidation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> dict:
    """Persist a compact summary for attempts since the previous finish."""
    conv = runs.get_conversation(conversation_id)
    if conv is None:
        raise AppError("CONVERSATION_NOT_FOUND", "对话不存在", status_code=404)
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    if conv.activity_type == "LEARN":
        raise AppError("ACTIVITY_MISMATCH", "资料问答不需要结束巩固", status_code=409)

    judgments: list[dict] = []
    for message in runs.messages_for(conversation_id):
        for block in message.content_blocks:
            if block.type == "status" and block.data.get("kind") == "consolidation_summary":
                judgments = []
            if block.type == "task" and block.data.get("kind") == "judgment" and not block.data.get("needs_review"):
                judgments.append(block.data.get("judgment") or {})
    counts = {"PASS": 0, "PARTIAL": 0, "FAIL": 0}
    for judgment in judgments:
        result = judgment.get("result")
        if result in counts:
            counts[result] += 1
    total = sum(counts.values())
    text = (
        f"本次完成 {total} 题：通过 {counts['PASS']}，部分通过 {counts['PARTIAL']}，"
        f"未通过 {counts['FAIL']}。学习状态只依据实际作答证据更新。"
        if total else "本次还没有完成题目。可以从左侧候选知识点开始一道练习或检测。"
    )
    message = Message(
        message_id=f"msg_{uuid.uuid4().hex[:12]}",
        conversation_id=conversation_id,
        role="assistant",
        content_blocks=[ContentBlock(
            type="status", text=text,
            data={"kind": "consolidation_summary", "total": total, "counts": counts},
        )],
    )
    runs.add_message(message)
    return {"message_id": message.message_id, "total": total, "counts": counts, "text": text}


@router.post("/conversations/{conversation_id}/tasks/{task_id}/explanation")
def explain_completed_task(
    conversation_id: str,
    task_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    qa: BookQAService = Depends(get_qa_service),
) -> dict:
    """Explain a completed task without fabricating a user chat message."""
    conv = runs.get_conversation(conversation_id)
    if conv is None:
        raise AppError("CONVERSATION_NOT_FOUND", "对话不存在", status_code=404)
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    task = _load_owned_task(repo, task_id, user)
    if task["project_id"] != conv.project_id:
        raise AppError("TASK_NOT_IN_PROJECT", "题目不属于当前学习空间", status_code=404)
    if task.get("status") == "PENDING":
        raise AppError("TASK_NOT_COMPLETED", "请先完成或跳过这道题，再查看讲解", status_code=409)

    target_ids = set(task.get("target_concept_ids") or [])
    concepts = [
        concept for source_id in repo.allowed_book_ids(conv.project_id)
        for concept in repo.concepts_for_book(source_id)
        if concept.concept_id in target_ids
    ]
    names = "、".join(concept.name for concept in concepts) or "本题知识点"
    source_ids = sorted({concept.book_id for concept in concepts}) or None
    answer = qa.ask(
        project_id=conv.project_id,
        learner_id=user.user_id,
        question=f"请结合资料讲解“{names}”，并说明刚才这道题应如何思考。",
        source_ids=source_ids,
    )
    blocks: list[ContentBlock] = []
    if answer.chunk_ids:
        items = []
        for chunk_id in answer.chunk_ids[:4]:
            chunk = repo.chunk_by_id(chunk_id)
            if chunk is None:
                continue
            source = repo.get_source(chunk.book_id)
            items.append({
                "source_id": chunk.book_id,
                "title": source.title if source else "学习资料",
                "locator": chunk.short_label(),
            })
        blocks.append(ContentBlock(type="context", data={
            "kind": "answer_context", "scope": "本题相关资料",
            "reason": "根据本题考查的知识点回到资料检索后生成讲解。",
            "items": items,
        }))
    blocks.append(ContentBlock(
        type="text",
        text=answer.answer_text or "暂时没有找到足够可靠的资料依据。可以回到原文后再提问。",
    ))
    for index, citation in enumerate(answer.citations):
        chunk_id = citation.get("chunk_id", "")
        chunk = repo.chunk_by_id(chunk_id) if chunk_id else None
        source_id = citation.get("book_id") or (chunk.book_id if chunk else "")
        page = citation.get("page") or (str(chunk.source_ref.physical_page) if chunk else "")
        source = repo.get_source(source_id) if source_id else None
        blocks.append(ContentBlock(
            type="citation", chunk_id=chunk_id, quote=citation.get("quote", ""),
            page=str(page), book_id=source_id,
            label=f"[{index + 1}] {source.title if source else '学习资料'} · {chunk.short_label() if chunk else f'p.{page}'}",
        ))
    message = Message(
        message_id=f"msg_{uuid.uuid4().hex[:12]}", conversation_id=conversation_id,
        role="assistant", content_blocks=blocks,
    )
    runs.add_message(message)
    return {"message_id": message.message_id, "grounded": answer.grounded}


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    _load_owned_task(repo, task_id, user)
    payload = tasks.get_task(task_id)
    if payload is None:
        raise AppError("TASK_NOT_FOUND", "任务不存在", status_code=404)
    return payload


@router.post("/tasks/{task_id}/answer")
def submit_answer(
    task_id: str,
    body: AnswerBody,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    _load_owned_task(repo, task_id, user)
    return tasks.submit_answer(
        task_id=task_id, answer_text=body.answer_text,
        idempotency_key=body.idempotency_key or "",
        learner_id=user.user_id,
    )


@router.post("/tasks/{task_id}/hint")
def request_hint(
    task_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    _load_owned_task(repo, task_id, user)
    return tasks.request_hint(task_id)


@router.post("/tasks/{task_id}/skip")
def skip_task(
    task_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    _load_owned_task(repo, task_id, user)
    return tasks.skip_task(task_id)
