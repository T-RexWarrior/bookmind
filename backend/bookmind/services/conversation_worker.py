"""Independent chat work pool used by the asynchronous message API."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ..domain.models import ContentBlock, Conversation, Message, Run, RunEvent
from ..storage.protocols import Repository
from .conversation_orchestrator import ConversationOrchestrator
from .run_service import RunService


class ConversationWorker:
    def __init__(self, repo: Repository, *, max_workers: int = 4) -> None:
        self.repo = repo
        self.runs = RunService(repo)
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="bookmind-chat")
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    def submit(
        self, *, orchestrator: ConversationOrchestrator, conversation: Conversation,
        user_text: str, learner_id: str, project_id: str, idempotency_key: str,
        history: list[Message], source_context: dict,
    ) -> tuple[Run, Message, str]:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        user_msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        assistant_msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        user_blocks = [ContentBlock(type="text", text=user_text)]
        selection = (source_context.get("selection_text") or "").strip()
        if selection:
            user_blocks.append(ContentBlock(type="context", data={
                "kind": "selection_context", "quote": selection,
                "source_id": source_context.get("source_id", ""),
                "page": source_context.get("page"),
            }))
        user_message = Message(
            message_id=user_msg_id, conversation_id=conversation.conversation_id,
            role="user", content_blocks=user_blocks,
        )
        run = Run(
            run_id=run_id, conversation_id=conversation.conversation_id,
            message_id=user_msg_id, status="QUEUED", idempotency_key=idempotency_key,
        )
        self.runs.add_message(user_message)
        self.runs.save_run(run)
        self.runs.save_events([RunEvent(
            run_id=run_id, sequence=0, event_type="run_started",
            payload={"user_message_id": user_msg_id, "status": "QUEUED"},
        )])
        self._executor.submit(
            self._execute, orchestrator, conversation, user_text, learner_id,
            project_id, idempotency_key, history, source_context,
            run_id, user_msg_id, assistant_msg_id,
        )
        return run, user_message, assistant_msg_id

    def _execute(
        self, orchestrator: ConversationOrchestrator, conversation: Conversation,
        user_text: str, learner_id: str, project_id: str, idempotency_key: str,
        history: list[Message], source_context: dict, run_id: str,
        user_msg_id: str, assistant_msg_id: str,
    ) -> None:
        queued = self.runs.get_run(run_id)
        if queued is None or self.is_cancelled(run_id):
            return
        self.runs.save_run(queued.model_copy(update={
            "status": "RUNNING", "started_at": datetime.now(timezone.utc),
        }))
        try:
            result = orchestrator.process_message(
                conversation=conversation, user_text=user_text,
                learner_id=learner_id, project_id=project_id,
                idempotency_key=idempotency_key, history=history,
                source_context=source_context, run_id=run_id,
                user_message_id=user_msg_id, assistant_message_id=assistant_msg_id,
                event_sink=self._persist_live_event,
            )
            if self.is_cancelled(run_id):
                return
            self.runs.add_message(result.assistant_message)
            # Sequence zero was published before returning HTTP 202.
            existing_sequences = {event.sequence for event in self.runs.events_for(run_id)}
            self.runs.save_events([
                event for event in result.events
                if event.sequence > 0 and event.sequence not in existing_sequences
            ])
            self.runs.save_run(result.run)
        except Exception:  # noqa: BLE001 — a worker failure must terminate the run
            if self.is_cancelled(run_id):
                return
            existing = self.runs.events_for(run_id)
            sequence = max((event.sequence for event in existing), default=-1) + 1
            failed = (self.runs.get_run(run_id) or queued).model_copy(update={
                "status": "FAILED", "error": "聊天任务执行失败",
                "completed_at": datetime.now(timezone.utc),
            })
            self.runs.save_events([RunEvent(
                run_id=run_id, sequence=sequence, event_type="run_failed",
                payload={"error": "这次没有处理成功，你可以稍后重试。"},
            )])
            self.runs.save_run(failed)

    def _persist_live_event(self, event: RunEvent) -> None:
        if self.is_cancelled(event.run_id):
            return
        # The browser treats run_completed as the signal to reload the
        # authoritative conversation.  Persisting that event while the worker
        # is still about to write the assistant Message creates a race: the
        # reload can observe only streamed text and miss context / learning
        # record cards.  All other events remain live; the terminal event is
        # saved immediately after add_message() in _execute above.
        if event.event_type == "run_completed":
            return
        self.runs.save_events([event])

    def cancel(self, run_id: str) -> Run | None:
        run = self.runs.get_run(run_id)
        if run is None:
            return None
        if run.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return run
        with self._lock:
            self._cancelled.add(run_id)
        cancelled = run.model_copy(update={
            "status": "CANCELLED", "completed_at": datetime.now(timezone.utc),
        })
        existing = self.runs.events_for(run_id)
        sequence = max((event.sequence for event in existing), default=-1) + 1
        self.runs.save_run(cancelled)
        self.runs.save_events([RunEvent(
            run_id=run_id, sequence=sequence, event_type="run_cancelled",
            payload={"run_status": "CANCELLED"},
        )])
        return cancelled

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancelled

    def stop(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["ConversationWorker"]
