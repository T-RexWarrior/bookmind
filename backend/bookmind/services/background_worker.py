"""Background ingestion worker — a single thread + queue (PRODUCTIZATION M3,
§18: no Redis/Celery/multi-worker).

One background thread pulls job ids off a :class:`queue.Queue` and runs each
through :class:`~bookmind.services.ingestion_runner.IngestionRunner`. The
upload route enqueues a job id and returns immediately; the worker continues
after the response is sent and across page closes (it lives in the server
process). On startup, :meth:`start` re-enqueues leftover PENDING jobs so they
resume from their last checkpoint.

Only one job runs at a time (single worker) — ingestion is CPU/IO bound and
the product targets a single machine. Cancellation is cooperative: a job whose
state was set to ``CANCELLED`` is skipped when dequeued.
"""

from __future__ import annotations

import logging
import queue
import threading

from .ingestion_runner import IngestionRunner
from .job_service import JobService

log = logging.getLogger("bookmind.worker")


class BackgroundWorker:
    """A single background thread draining an ingestion-job queue."""

    def __init__(self, runner: IngestionRunner, jobs: JobService) -> None:
        self.runner = runner
        self.jobs = jobs
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Recover leftover RUNNING jobs → PENDING, then re-enqueue all PENDING.
        self.jobs.recover_on_startup()
        for job in self._all_pending():
            self._queue.put(job.job_id)
        self._thread = threading.Thread(target=self._loop, name="bookmind-ingestion", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._queue.put(None)  # sentinel to unblock the dequeue
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def enqueue(self, job_id: str) -> None:
        self._queue.put(job_id)

    # --- internals ---------------------------------------------------------

    def _all_pending(self):
        """Return pending jobs through the persistence boundary."""
        return self.jobs.pending_jobs()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if job_id is None:
                break
            try:
                self.runner.run_job(job_id)
            except Exception:  # noqa: BLE001 — never kill the worker thread
                log.exception("Worker failed on job %s", job_id)
