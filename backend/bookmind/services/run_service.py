"""Run service — persists conversations / messages / runs / run_events and
streams them as SSE (PRODUCTIZATION M2, §8.4).

All persistence goes through the Repository protocol; this service has no
knowledge of SQLAlchemy or the in-memory implementation. SSE follows the spec
in §8.4: each event has run_id, sequence, timestamp; ``Last-Event-ID``
resumes by replaying missed events.
"""

from __future__ import annotations

import json
from typing import Iterator, Literal

from ..domain.models import Conversation, Message, Run, RunEvent
from ..storage.protocols import Repository


class RunService:
    """Owns conversation / message / run / run_event persistence."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    # --- conversations ------------------------------------------------------

    def create_conversation(
        self,
        *,
        project_id: str,
        title: str = "",
        activity_type: Literal["LEARN", "REVIEW", "ASSESSMENT"] = "LEARN",
    ) -> Conversation:
        import uuid
        conv = Conversation(
            conversation_id=f"conv_{uuid.uuid4().hex[:12]}",
            project_id=project_id, activity_type=activity_type, title=title or "新对话",
        )
        self.repo.save_conversation(conv)
        return conv

    def list_conversations(
        self,
        project_id: str,
        activity_type: Literal["LEARN", "REVIEW", "ASSESSMENT"] | None = None,
    ) -> list[Conversation]:
        return self.repo.conversations_for_project(project_id, activity_type)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.repo.get_conversation_record(conversation_id)

    def rename_conversation(self, conversation_id: str, new_title: str) -> Conversation | None:
        """Soft-rename a conversation (PRODUCTIZATION §5.12). Returns the
        updated conversation, or None if it does not exist (or is deleted)."""
        title = (new_title or "").strip() or "新对话"
        return self.repo.rename_conversation_record(conversation_id, title)

    def delete_conversation(self, conversation_id: str) -> bool:
        """Soft-delete a conversation. Deleting a conversation does NOT delete
        Evidence or project learning state (PRODUCTIZATION §5.12). Returns True
        if a live conversation was deleted, False if it was already gone."""
        return self.repo.delete_conversation_record(conversation_id)

    # --- messages -----------------------------------------------------------

    def add_message(self, message: Message) -> None:
        self.repo.save_message(message)

    def messages_for(self, conversation_id: str) -> list[Message]:
        return self.repo.messages_for_conversation(conversation_id)

    # --- runs & events ------------------------------------------------------

    def save_run(self, run: Run) -> None:
        self.repo.save_run_record(run)

    def get_run(self, run_id: str) -> Run | None:
        return self.repo.get_run_record(run_id)

    def find_run_by_idempotency(self, conversation_id: str, idempotency_key: str) -> Run | None:
        """P1-12: locate a prior run by (conversation_id, idempotency_key) so a
        retried send replays the existing result instead of creating a new run
        + duplicate messages. Returns None if no such run exists."""
        if not idempotency_key:
            return None
        return self.repo.find_run_record(conversation_id, idempotency_key)

    def save_events(self, events: list[RunEvent]) -> None:
        if not events:
            return
        self.repo.save_run_event_records(events)

    def events_for(self, run_id: str, *, after_sequence: int = -1) -> list[RunEvent]:
        return self.repo.run_event_records(run_id, after_sequence=after_sequence)

    # --- SSE stream ---------------------------------------------------------

    def sse_stream(self, run_id: str, *, last_event_id: int | None = None) -> Iterator[str]:
        """Yield SSE-formatted lines for a run's events.

        ``last_event_id`` (from the ``Last-Event-ID`` header) resumes after the
        given sequence. Events already persisted are replayed, then the stream
        ends (this is a historical replay model — sufficient for M2 where the
        run completes synchronously before streaming begins)."""
        after = last_event_id if last_event_id is not None else -1
        for ev in self.events_for(run_id, after_sequence=after):
            data = {
                "run_id": ev.run_id, "sequence": ev.sequence,
                "timestamp": ev.created_at.isoformat(),
                "event_type": ev.event_type,
                **ev.payload,
            }
            yield f"event: {ev.event_type}\n"
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n"
            yield f"id: {ev.sequence}\n\n"
