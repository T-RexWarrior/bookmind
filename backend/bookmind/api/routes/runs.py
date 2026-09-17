"""Run routes — SSE event stream + cancel (PRODUCTIZATION §8.2, §8.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from ...domain.models import User
from ...storage.protocols import Repository
from ..dependencies import get_conversation_worker, get_current_user, get_repo, get_run_service
from ...services.run_service import RunService
from ...services.conversation_worker import ConversationWorker
from ...services.run_trace import build_run_trace, render_trace_markdown
from ...config import get_settings

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _owned_run(run_id: str, user: User, repo: Repository, runs: RunService):
    """Load a run and enforce the same owner boundary for every trace view."""
    run = runs.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    conv = runs.get_conversation(run.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    return run


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
    run = _owned_run(run_id, user, repo, runs)

    last_event_id = request.headers.get("last-event-id")
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else None

    def stream():
        yield from runs.sse_stream(run_id, last_event_id=after)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/{run_id}/trace")
def run_trace(
    run_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> dict:
    """Return a redacted, replayable decision trace for one completed or live run."""
    run = _owned_run(run_id, user, repo, runs)
    return build_run_trace(run, runs.events_for(run_id))


@router.get("/{run_id}/trace/debug")
def run_trace_debug(
    run_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> dict:
    """Local opt-in diagnostic trace, including captured model I/O when enabled."""
    if not get_settings().trace_capture_content:
        raise HTTPException(status_code=404, detail="sensitive trace capture is disabled")
    run = _owned_run(run_id, user, repo, runs)
    return build_run_trace(run, runs.events_for(run_id), include_sensitive=True)


@router.get("/{run_id}/trace/markdown", response_class=PlainTextResponse)
def run_trace_markdown(
    run_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> PlainTextResponse:
    """Export the same trace as a compact Markdown timeline for a defense deck."""
    run = _owned_run(run_id, user, repo, runs)
    trace = build_run_trace(run, runs.events_for(run_id))
    return PlainTextResponse(
        render_trace_markdown(trace),
        media_type="text/markdown; charset=utf-8",
    )


@router.post("/{run_id}/cancel")
def cancel_run(
    run_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    worker: ConversationWorker = Depends(get_conversation_worker),
) -> dict:
    run = _owned_run(run_id, user, repo, runs)
    cancelled = worker.cancel(run_id)
    return {"run_id": run_id, "status": cancelled.status if cancelled else run.status}
