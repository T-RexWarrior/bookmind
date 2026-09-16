"""Job service — persistent, resumable ingestion jobs (PRODUCTIZATION M3,
ARCHITECTURE §11).

Owns the ingestion-job use case and its SSE event stream. Persistence goes
through the Repository protocol, so this service has no SQL/in-memory branch
and never opens an ORM session itself.

The five user-facing stages (PRODUCTIZATION §5.3) are a projection of the
internal ``JobStage`` enum — see :func:`user_stage`. Progress comes from real
stage checkpoints written by :class:`~bookmind.services.ingestion_runner.IngestionRunner`,
never from a timer.

Job progress SSE reuses the existing :class:`~bookmind.domain.enums.EventType`
vocabulary (no synonymous events are added): ``tool_started``/``tool_completed``
mark stage transitions, ``run_completed`` marks DONE, ``run_failed`` marks
failure.
"""

from __future__ import annotations

import json
import uuid
import time
from datetime import datetime, timezone
from typing import Iterator

from ..domain.enums import EventType
from ..jobs.job_store import IngestionJob, JobStage, JobState
from ..storage.protocols import Repository

# User-facing five-stage labels (PRODUCTIZATION §5.3). Internal stages map onto
# these; the frontend renders this exact order.
USER_STAGES: list[dict] = [
    {"key": "upload", "label": "上传资料", "progress": 0.05},
    {"key": "parse", "label": "识别页面与章节", "progress": 0.30},
    {"key": "index", "label": "建立可检索内容", "progress": 0.65},
    {"key": "graph", "label": "整理资料知识范围", "progress": 0.90},
    {"key": "done", "label": "准备完成", "progress": 1.00},
]


def user_stage(job: IngestionJob) -> dict:
    """Project a job's internal state onto the five user-facing stages.

    Returns ``{stage: <key>, label: <中文>, progress: <0..1>, done: bool}``.
    """
    stage, progress = job.stage, job.progress
    if job.state == JobState.SUCCEEDED or stage == JobStage.DONE:
        return {**USER_STAGES[-1]}
    if stage in (JobStage.QUEUED,):
        return {**USER_STAGES[0]}
    if stage == JobStage.PARSING:
        return {**USER_STAGES[1]}
    if stage in (JobStage.CHUNKING, JobStage.INDEXING):
        return {**USER_STAGES[2]}
    if stage == JobStage.GRAPH:
        return {**USER_STAGES[3]}
    return {**USER_STAGES[0]}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobService:
    """Persistent ingestion jobs + progress SSE."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        # Job progress events: run_id(job_id) -> list[event dict]. Short-lived;
        # a restart resumes progress from the DB checkpoint, not event replay.
        self._events: dict[str, list[dict]] = {}

    # --- create / read / update --------------------------------------------

    def create(self, *, project_id: str, book_id: str, source_hash: str,
               filename: str) -> IngestionJob:
        job = IngestionJob(
            job_id=f"job_{uuid.uuid4().hex[:12]}",
            project_id=project_id, book_id=book_id,
            source_hash=source_hash, filename=filename or "upload.pdf",
            state=JobState.PENDING, stage=JobStage.QUEUED, progress=0.05,
        )
        self._persist(job)
        return job

    def get(self, job_id: str) -> IngestionJob | None:
        return self.repo.get_ingestion_job(job_id)

    def jobs_for_project(self, project_id: str) -> list[IngestionJob]:
        return self.repo.ingestion_jobs_for_project(project_id)

    def job_for_book(self, book_id: str, project_id: str | None = None) -> IngestionJob | None:
        return self.repo.latest_ingestion_job_for_book(book_id, project_id)

    def update(self, job: IngestionJob) -> None:
        job.touch()
        self._persist(job)

    def _persist(self, job: IngestionJob) -> None:
        self.repo.save_ingestion_job(job)

    # --- recovery ----------------------------------------------------------

    def recover_on_startup(self) -> list[IngestionJob]:
        """Move leftover RUNNING jobs back to PENDING so the worker re-runs them
        (ARCHITECTURE §11). Returns the recovered jobs."""
        return self.repo.recover_running_ingestion_jobs()

    def pending_jobs(self) -> list[IngestionJob]:
        return self.repo.pending_ingestion_jobs()

    # --- events / SSE ------------------------------------------------------

    def emit(self, job_id: str, event_type: str, payload: dict | None = None) -> dict:
        """Record a progress event for a job and return it."""
        seq = len(self._events.get(job_id, [])) + 1
        ev = {
            "job_id": job_id, "sequence": seq, "event_type": event_type,
            "timestamp": _now().isoformat(),
            **(payload or {}),
        }
        self._events.setdefault(job_id, []).append(ev)
        return ev

    def events_for(self, job_id: str, *, after_sequence: int = -1) -> list[dict]:
        return [e for e in self._events.get(job_id, []) if e["sequence"] > after_sequence]

    def sse_stream(self, job_id: str, *, last_event_id: int | None = None) -> Iterator[str]:
        after = last_event_id if last_event_id is not None else -1
        while True:
            events = self.events_for(job_id, after_sequence=after)
            for ev in events:
                yield f"event: {ev['event_type']}\n"
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n"
                yield f"id: {ev['sequence']}\n\n"
                after = max(after, ev["sequence"])
            job = self.get(job_id)
            if job is None or job.state in {
                JobState.SUCCEEDED, JobState.FAILED,
                JobState.RETRYABLE_FAILED, JobState.CANCELLED,
            }:
                break
            if not events:
                yield ": keep-alive\n\n"
            time.sleep(0.25)
