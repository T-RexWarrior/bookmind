"""Task routes — /api/tasks/{task_id} (PRODUCTIZATION §8.2, §M4).

The browser interacts with a server-owned task by ``task_id`` only. Answer
submission accepts ``answer_text`` + ``idempotency_key`` and nothing else — no
PASS/FAIL, no Concept ID, no rubric, no hint count, no misconception score. The
trusted task, the rubric, and the hint tally all live server-side; the
Diagnostician judges; the Learning Engine writes state.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from ...domain.enums import EventType, UIPreset
from ...domain.models import ContentBlock, Message, Run, RunEvent, User
from ...storage.protocols import Repository
from ..dependencies import get_current_user, get_repo, get_run_service, get_task_service
from ..errors import AppError
from ...services.run_service import RunService
from ...services.task_service import TaskService
from ...config import get_settings

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


class FollowupBody(BaseModel):
    question: str

    @field_validator("question")
    @classmethod
    def limit_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question is empty")
        if len(value) > 4_000:
            raise ValueError("question is too long")
        return value


class CreateTaskBody(BaseModel):
    """Explicit UI command for starting a consolidation task.

    This is intentionally separate from chat messages: button clicks send
    structured intent and never rely on the model to infer control text.
    """

    mode: Literal["PRACTICE", "ASSESSMENT"]
    selection: Literal["RECOMMENDED", "QUESTIONED", "WEAK", "DUE", "UNVERIFIED", "ALL", "RANDOM"] = "RANDOM"
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


def _trace_task_operation(runs: RunService, tasks: TaskService, *, conversation_id: str, action: str, operation):
    """Give every exercise-button operation an auditable run boundary."""
    now = datetime.now(timezone.utc)
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    run = Run(
        run_id=run_id, conversation_id=conversation_id, message_id=f"taskop_{uuid.uuid4().hex[:12]}",
        status="RUNNING", intent=action, started_at=now,
    )
    events = [
        RunEvent(run_id=run_id, sequence=0, event_type=EventType.RUN_STARTED.value,
                 payload={"run_status": "RUNNING"}, created_at=now),
        RunEvent(run_id=run_id, sequence=1, event_type=EventType.ACTION_SELECTED.value,
                 payload={"intent": action, "workflow": "task_button"}, created_at=now),
    ]
    runs.save_run(run)
    runs.save_events(events)
    try:
        with tasks.router.trace_capture(
            run_id, capture_content=get_settings().trace_capture_content,
        ) as calls:
            result = operation(run_id)
        sequence = 2
        for call in calls:
            events.append(RunEvent(run_id=run_id, sequence=sequence, event_type=EventType.LLM_CALL.value,
                                   payload=call, created_at=datetime.now(timezone.utc)))
            sequence += 1
        events.append(RunEvent(
            run_id=run_id, sequence=sequence, event_type=EventType.TOOL_COMPLETED.value,
            payload={"tool": action.lower(), "result": "completed"}, created_at=datetime.now(timezone.utc),
        ))
        sequence += 1
        state_delta = result.get("state_delta", {}) if isinstance(result, dict) else {}
        if state_delta.get("mastery_transitions") or state_delta.get("misconception_transitions"):
            events.append(RunEvent(
                run_id=run_id, sequence=sequence, event_type=EventType.STATE_UPDATED.value,
                payload=state_delta, created_at=datetime.now(timezone.utc),
            ))
            sequence += 1
        completed = run.model_copy(update={"status": "COMPLETED", "completed_at": datetime.now(timezone.utc)})
        events.append(RunEvent(run_id=run_id, sequence=sequence, event_type=EventType.RUN_COMPLETED.value,
                               payload={"run_status": "COMPLETED"}, created_at=datetime.now(timezone.utc)))
        runs.save_events(events[2:])
        runs.save_run(completed)
        return result, run_id
    except Exception:
        failed = run.model_copy(update={"status": "FAILED", "completed_at": datetime.now(timezone.utc),
                                        "error": "task button operation failed"})
        runs.save_events([RunEvent(run_id=run_id, sequence=2, event_type=EventType.RUN_FAILED.value,
                                   payload={"status": "failed"}, created_at=datetime.now(timezone.utc))])
        runs.save_run(failed)
        raise


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
    active_followup = repo.active_followup_task_for_conversation(conversation_id)
    if active_followup is not None:
        raise AppError(
            "FOLLOWUP_ACTIVE",
            "请先结束上一题的追问，再开始下一题。",
            status_code=409,
            action="END_FOLLOWUP",
        )
    selected_concept_id = body.concept_id
    if body.from_task_id:
        source_task = _load_owned_task(repo, body.from_task_id, user)
        if source_task["project_id"] != conv.project_id:
            raise AppError("TASK_NOT_IN_PROJECT", "原题不属于当前学习空间", status_code=404)
        previous_concept_id = next(iter(source_task.get("target_concept_ids") or []), "")
        previous_state = repo.get_state(conv.project_id, previous_concept_id)
        # “下一题” is a continuation command.  Keep advancing the current
        # concept through L1→L4; only after L4 choose the next mapped sibling
        # instead of silently falling back to the first global candidate.
        selected_concept_id = (
            tasks.next_practice_concept_after(conv.project_id, previous_concept_id)
            if previous_state.current_verified_level.value == "L4"
            else previous_concept_id
        )
    if selected_concept_id and not repo.concept_in_project_scope(selected_concept_id, conv.project_id):
        raise AppError("CONCEPT_NOT_IN_SCOPE", "这个知识点不在当前学习空间中", status_code=404)

    pending = repo.pending_task_for_conversation(conversation_id)
    repo.update_project(
        conv.project_id,
        default_mode=UIPreset.REVIEW if body.mode == "PRACTICE" else UIPreset.ASSESSMENT,
    )
    payload, run_id = _trace_task_operation(
        runs, tasks, conversation_id=conversation_id, action="REQUEST_TASK",
        operation=lambda _trace_run_id: tasks.request_task(
            project_id=conv.project_id, learner_id=user.user_id,
            conversation_id=conversation_id, concept_id=selected_concept_id, selection=body.selection,
        ),
    )
    safe_fields = (
        "kind", "task_id", "prompt_text", "is_probe", "is_changed_task",
        "remediation_stage", "focus", "source_scope", "generation_reason",
        "generation_mode", "generation_notice", "hints_issued", "status", "text", "existing",
    )
    payload = {key: payload[key] for key in safe_fields if key in payload}
    if pending is not None:
        return {"task": payload, "message_id": None, "existing": True, "run_id": run_id}

    if payload.get("task_id"):
        block = ContentBlock(type="task", data=payload)
    else:
        block = ContentBlock(type="error", text=payload.get("text", "暂时无法生成题目。"))
    message = Message(
        message_id=f"msg_{uuid.uuid4().hex[:12]}",
        conversation_id=conversation_id,
        role="assistant", run_id=run_id,
        content_blocks=[block],
    )
    runs.add_message(message)
    # Return the rendered message as well as its id.  This avoids a second
    # read-after-write round trip in the client, which previously made a
    # successful button click look like it had done nothing when the refresh
    # raced with a mode switch.
    return {
        "task": payload,
        "message_id": message.message_id,
        "message": {
            "message_id": message.message_id,
            "role": message.role,
            "content_blocks": [block.model_dump() for block in message.content_blocks],
            "run_id": message.run_id,
            "created_at": message.created_at.isoformat(),
        },
        "existing": False,
        "run_id": run_id,
    }


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
    if repo.active_followup_task_for_conversation(conversation_id) is not None:
        raise AppError(
            "FOLLOWUP_ACTIVE",
            "请先结束上一题的追问，再结束本次练习。",
            status_code=409,
            action="END_FOLLOWUP",
        )

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
def explain_task(
    conversation_id: str,
    task_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    """Reveal a task-specific explanation and, if needed, close the attempt."""
    conv = runs.get_conversation(conversation_id)
    if conv is None:
        raise AppError("CONVERSATION_NOT_FOUND", "对话不存在", status_code=404)
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    task = _load_owned_task(repo, task_id, user)
    if task["project_id"] != conv.project_id:
        raise AppError("TASK_NOT_IN_PROJECT", "题目不属于当前学习空间", status_code=404)
    explanation, run_id = _trace_task_operation(
        runs, tasks, conversation_id=conversation_id, action="REQUEST_EXPLANATION",
        operation=lambda _trace_run_id: tasks.explain_task(task_id),
    )
    blocks: list[ContentBlock] = []
    source_scope = explanation.get("source_scope") or []
    if source_scope:
        blocks.append(ContentBlock(type="context", data={
            "kind": "answer_context", "scope": "本题资料定位",
            "reason": "讲解依据本题的题干、标准答案和判分要点生成；资料定位仅供回原文复习。",
            "items": source_scope,
        }))
    if explanation.get("revealed_while_pending"):
        blocks.append(ContentBlock(
            type="status",
            text="已展示讲解并结束本题；这次不会记为作答证据。可以开始下一题重新独立练习。",
        ))
    blocks.append(ContentBlock(
        type="text",
        text=explanation["text"],
    ))
    blocks.append(ContentBlock(type="task", data={
        "kind": "task_complete",
        "task_id": task_id,
        "completion_status": explanation.get("completion_status", "EXPLAINED"),
        "source_scope": source_scope,
    }))
    message = Message(
        message_id=f"msg_{uuid.uuid4().hex[:12]}", conversation_id=conversation_id,
        role="assistant", content_blocks=blocks, run_id=run_id,
    )
    runs.add_message(message)
    return {
        "message_id": message.message_id,
        "revealed_while_pending": explanation["revealed_while_pending"],
        "run_id": run_id,
    }


@router.post("/conversations/{conversation_id}/tasks/{task_id}/followup")
def follow_up_task(
    conversation_id: str,
    task_id: str,
    body: FollowupBody,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    """Read-only task follow-up; it never writes learning evidence."""
    conv = runs.get_conversation(conversation_id)
    if conv is None:
        raise AppError("CONVERSATION_NOT_FOUND", "对话不存在", status_code=404)
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    task = _load_owned_task(repo, task_id, user)
    if task["project_id"] != conv.project_id:
        raise AppError("TASK_NOT_IN_PROJECT", "题目不属于当前学习空间", status_code=404)
    if task.get("conversation_id") != conversation_id:
        raise AppError("TASK_NOT_IN_CONVERSATION", "题目不属于当前对话", status_code=404)
    # Keep the message endpoint backward-compatible for clients that send the
    # first follow-up directly, while the current UI explicitly opens the
    # phase as soon as the learner clicks “追问本题”.
    tasks.start_followup(task_id)
    answer, run_id = _trace_task_operation(
        runs, tasks, conversation_id=conversation_id, action="TASK_FOLLOWUP",
        operation=lambda _trace_run_id: tasks.answer_followup(task_id, body.question),
    )
    from ...services.learning_memory import remember_task_followup
    remember_task_followup(
        repo, project_id=conv.project_id, conversation_id=conversation_id,
        task_id=task_id, question=body.question,
    )
    message = Message(
        message_id=f"msg_{uuid.uuid4().hex[:12]}", conversation_id=conversation_id,
        role="assistant", run_id=run_id, content_blocks=[
            ContentBlock(type="status", text="题后追问不会影响学习状态。"),
            ContentBlock(type="text", text=answer),
        ],
    )
    runs.add_message(message)
    return {"message_id": message.message_id, "text": answer, "read_only": True, "run_id": run_id}


@router.post("/conversations/{conversation_id}/tasks/{task_id}/followup/start")
def start_task_followup(
    conversation_id: str,
    task_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    """Open a durable follow-up phase before the learner types a question."""
    conv = runs.get_conversation(conversation_id)
    if conv is None:
        raise AppError("CONVERSATION_NOT_FOUND", "对话不存在", status_code=404)
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    task = _load_owned_task(repo, task_id, user)
    if task["project_id"] != conv.project_id or task.get("conversation_id") != conversation_id:
        raise AppError("TASK_NOT_IN_CONVERSATION", "题目不属于当前对话", status_code=404)
    return tasks.start_followup(task_id)


@router.post("/conversations/{conversation_id}/tasks/{task_id}/followup/close")
def close_task_followup(
    conversation_id: str,
    task_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    conv = runs.get_conversation(conversation_id)
    if conv is None:
        raise AppError("CONVERSATION_NOT_FOUND", "对话不存在", status_code=404)
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    task = _load_owned_task(repo, task_id, user)
    if task["project_id"] != conv.project_id or task.get("conversation_id") != conversation_id:
        raise AppError("TASK_NOT_IN_CONVERSATION", "题目不属于当前对话", status_code=404)
    return tasks.end_followup(task_id)


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
    runs: RunService = Depends(get_run_service),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    task = _load_owned_task(repo, task_id, user)
    result, run_id = _trace_task_operation(
        runs, tasks, conversation_id=task["conversation_id"], action="SUBMIT_ANSWER",
        operation=lambda trace_run_id: tasks.submit_answer(
            task_id=task_id, answer_text=body.answer_text,
            idempotency_key=body.idempotency_key or "", learner_id=user.user_id, run_id=trace_run_id,
        ),
    )
    return {**result, "run_id": run_id}


@router.post("/tasks/{task_id}/hint")
def request_hint(
    task_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    task = _load_owned_task(repo, task_id, user)
    result, run_id = _trace_task_operation(
        runs, tasks, conversation_id=task["conversation_id"], action="REQUEST_HINT",
        operation=lambda _trace_run_id: tasks.request_hint(task_id),
    )
    return {**result, "run_id": run_id}


@router.post("/tasks/{task_id}/skip")
def skip_task(
    task_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    tasks: TaskService = Depends(get_task_service),
) -> dict:
    task = _load_owned_task(repo, task_id, user)
    result, run_id = _trace_task_operation(
        runs, tasks, conversation_id=task["conversation_id"], action="SKIP_TASK",
        operation=lambda _trace_run_id: tasks.skip_task(task_id),
    )
    source_scope = tasks.source_scope_for_task(task_id)
    message = Message(
        message_id=f"msg_{uuid.uuid4().hex[:12]}", conversation_id=task["conversation_id"],
        role="assistant", run_id=run_id, content_blocks=[
            ContentBlock(type="text", text="已跳过这道题，不会把它记为错误答案。"),
            ContentBlock(type="task", data={
                "kind": "task_complete", "task_id": task_id,
                "completion_status": "SKIPPED", "source_scope": source_scope,
            }),
        ],
    )
    runs.add_message(message)
    return {**result, "message_id": message.message_id, "run_id": run_id}
