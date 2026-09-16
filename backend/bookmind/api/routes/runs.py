"""Run routes — SSE event stream + cancel (PRODUCTIZATION §8.2, §8.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...domain.models import User
from ...storage.protocols import Repository
from ..dependencies import get_conversation_worker, get_current_user, get_repo, get_run_service
from ...services.run_service import RunService
from ...services.conversation_worker import ConversationWorker

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("/{run_id}/events")
def run_events(
    run_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> StreamingResponse:
    """Stream a run's events as SSE. Supports ``Last-Event-ID`` for resume
    (PRODUCTIZATION §8.4: replay missed events from the store)."""
    run = runs.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    # Scope: the run's conversation must belong to the current user.
    conv = runs.get_conversation(run.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    repo.assert_project_owned_by(conv.project_id, user.user_id)

    last_event_id = request.headers.get("last-event-id")
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else None

    def stream():
        yield from runs.sse_stream(run_id, last_event_id=after)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/{run_id}/cancel")
def cancel_run(
    run_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    worker: ConversationWorker = Depends(get_conversation_worker),
) -> dict:
    run = runs.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    conv = runs.get_conversation(run.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    cancelled = worker.cancel(run_id)
    return {"run_id": run_id, "status": cancelled.status if cancelled else run.status}
