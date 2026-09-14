"""Learning-source and ingestion-job routes.
(PRODUCTIZATION M3, §8.2).

Upload is multipart (no Base64). The route validates, stores the file, creates
a PENDING job, enqueues it on the background worker, and returns immediately
with ``{book_id, job_id}`` — processing continues after the response. Job
progress is read via ``GET /api/jobs/{job_id}`` or its SSE stream. The PDF
itself is served for the Reader via ``GET /api/books/{book_id}/source.pdf``.

All requests derive ``user_id`` from the session cookie and scope-check book
access (§11.2).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, StreamingResponse

from ...domain.enums import BookRole
from ...domain.models import Book, ProjectBook, User
from ...config import Settings
from ...services.upload_service import UploadService
from ...services.job_service import JobService, user_stage
from ...services.background_worker import BackgroundWorker
from ...storage.protocols import Repository, ScopeError
from ..dependencies import (
    get_background_worker, get_current_user, get_job_service, get_repo,
    get_settings_dep, get_upload_service,
)
from ..errors import AppError

router = APIRouter(prefix="/api", tags=["sources"])


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- upload + list ---------------------------------------------------------

@router.post("/projects/{project_id}/sources")
@router.post("/projects/{project_id}/books", deprecated=True)
async def upload_book(
    project_id: str,
    file: UploadFile = File(...),
    title: str = Form(""),
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    upload: UploadService = Depends(get_upload_service),
    jobs: JobService = Depends(get_job_service),
    worker: BackgroundWorker = Depends(get_background_worker),
) -> dict:
    """Upload a PDF learning source. Creates a draft source + ingestion job
    and returns immediately; processing runs in the background."""
    repo.assert_project_owned_by(project_id, user.user_id)
    raw = await file.read()
    return _store_and_enqueue_pdf(
        project_id=project_id,
        raw=raw,
        filename=file.filename or "upload.pdf",
        content_type=file.content_type or "",
        title=title,
        user=user,
        repo=repo,
        upload=upload,
        jobs=jobs,
        worker=worker,
    )


@router.post("/projects/{project_id}/sources/sample")
@router.post("/projects/{project_id}/books/sample", deprecated=True)
def import_real_sample_book(
    project_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    upload: UploadService = Depends(get_upload_service),
    jobs: JobService = Depends(get_job_service),
    worker: BackgroundWorker = Depends(get_background_worker),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    """Import the configured real sample PDF through the normal upload path.

    Unlike the legacy deterministic Java corpus, this creates a real Book,
    stores the original PDF, and runs parsing/indexing/graph construction.
    The browser never receives or trusts a server filesystem path.
    """
    repo.assert_project_owned_by(project_id, user.user_id)
    path = settings.sample_book_file
    if not path.is_file():
        raise AppError(
            "SAMPLE_BOOK_MISSING",
            f"未找到示例资料 {path.name}，请检查 BOOKMIND_SAMPLE_BOOK_PATH。",
            status_code=404,
            can_retry=False,
            action="CONFIGURE_SAMPLE_BOOK",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AppError(
            "SAMPLE_BOOK_UNREADABLE",
            "示例资料无法读取，请检查文件权限。",
            status_code=500,
            can_retry=True,
            action="RETRY",
        ) from exc
    result = _store_and_enqueue_pdf(
        project_id=project_id,
        raw=raw,
        filename=path.name,
        content_type="application/pdf",
        title=settings.sample_book_title,
        user=user,
        repo=repo,
        upload=upload,
        jobs=jobs,
        worker=worker,
    )
    result["sample_kind"] = "real_pdf"
    return result


def _store_and_enqueue_pdf(
    *,
    project_id: str,
    raw: bytes,
    filename: str,
    content_type: str,
    title: str,
    user: User,
    repo: Repository,
    upload: UploadService,
    jobs: JobService,
    worker: BackgroundWorker,
) -> dict:
    """Shared real-file path for browser uploads and the configured sample."""
    upload.validate(raw, filename, content_type)

    source_hash = _sha256(raw)
    # P1-11: include the owner in the book_id so two users uploading the same
    # PDF get *distinct* book records (and never overwrite each other's owner).
    # Per-user dedup is preserved via _find_book_by_hash (filtered by owner).
    book_id = f"book_{hashlib.md5((user.user_id + '|' + source_hash).encode()).hexdigest()[:12]}"
    book_title = title or _strip_ext(filename)
    link_warning = ""

    # Duplicate-file reuse: if a book with this source_hash already exists for
    # this user, link the existing book to the project and skip re-processing.
    existing_book = _find_book_by_hash(repo, user.user_id, source_hash)
    reused = False
    if existing_book is not None:
        book_id = existing_book.book_id
        book_title = existing_book.title or book_title
        reused = True
        # Link to this project if not already linked (learning state is per-project).
        if book_id not in repo.allowed_book_ids(project_id):
            link_warning = _link_uploaded_book(repo, project_id, book_id)
        # If a prior job already succeeded, reflect that; otherwise enqueue.
        prior = jobs.job_for_book(book_id)
        if prior and prior.state.value == "SUCCEEDED" and not _graph_needs_rebuild(repo, book_id):
            scoped = jobs.job_for_book(book_id, project_id)
            if scoped is None:
                from ...jobs.job_store import JobStage, JobState
                scoped = jobs.create(
                    project_id=project_id, book_id=book_id,
                    source_hash=source_hash, filename=filename,
                )
                scoped.stage = JobStage.DONE
                scoped.progress = 1.0
                scoped.state = JobState.SUCCEEDED
                jobs.update(scoped)
            return {"source_id": book_id, "book_id": book_id,
                    "job_id": scoped.job_id, "reused": True,
                    "warning": link_warning or None}
    else:
        upload.save(user.user_id, book_id, raw, filename)
        repo.add_book(Book(
            book_id=book_id, owner_user_id=user.user_id,
            source_hash=source_hash, title=book_title,
            source_type="PDF", original_filename=filename,
        ))
        link_warning = _link_uploaded_book(repo, project_id, book_id)

    # Create + enqueue the ingestion job (skipped only if already succeeded).
    job = jobs.create(project_id=project_id, book_id=book_id,
                      source_hash=source_hash, filename=filename)
    worker.enqueue(job.job_id)
    return {"source_id": book_id, "book_id": book_id,
            "job_id": job.job_id, "reused": reused,
            "warning": link_warning or None}


def _link_uploaded_book(repo: Repository, project_id: str, book_id: str) -> str:
    """Always put an accepted upload inside the target project's scope.

    A project may already contain the demo or another PRIMARY textbook. In
    that case the new upload becomes a retrieval-enabled REFERENCE. Starting
    ingestion for an unlinked book would make graph construction fail later
    with a misleading scope error, so a failed fallback aborts synchronously.
    """
    try:
        repo.link_book(ProjectBook(
            project_id=project_id, book_id=book_id, role=BookRole.PRIMARY,
        ))
        return ""
    except ScopeError:
        # A concurrent request may have linked the exact book already.
        if book_id in repo.allowed_book_ids(project_id):
            return ""
        try:
            repo.link_book(ProjectBook(
                project_id=project_id, book_id=book_id, role=BookRole.REFERENCE,
            ))
        except Exception as exc:
            raise AppError(
                "BOOK_LINK_FAILED",
                "资料已保存，但无法关联到当前学习空间，请稍后重试。",
                status_code=409,
                can_retry=True,
                action="RETRY",
            ) from exc
        return "该学习空间已有核心资料，本次上传已作为补充资料加入并参与知识图谱构建。"


@router.get("/projects/{project_id}/sources")
@router.get("/projects/{project_id}/books", deprecated=True)
def list_books(
    project_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    jobs: JobService = Depends(get_job_service),
) -> list[dict]:
    """List a learning space's sources with processing and outline metadata."""
    repo.assert_project_owned_by(project_id, user.user_id)
    book_ids = sorted(repo.allowed_book_ids(project_id))
    out: list[dict] = []
    for bid in book_ids:
        # One physical PDF can be linked into several learning spaces, but its
        # progress belongs to the ingestion requested by this space.
        job = jobs.job_for_book(bid, project_id)
        book = _get_book(repo, bid)
        out.append({
            "source_id": bid,
            "book_id": bid,
            "title": book.title if book else "",
            "source_type": book.source_type if book else "PDF",
            "original_filename": book.original_filename if book else "",
            "page_count": book.page_count if book else 0,
            "section_count": book.section_count if book else 0,
            "outline": book.outline if book else [],
            "concept_count": len(repo.concepts_for_book(bid)),
            "job_id": job.job_id if job else None,
            "state": job.state.value if job else "UNKNOWN",
            "stage": user_stage(job)["label"] if job else "",
            "progress": job.progress if job else 0.0,
        })
    return out


@router.get("/projects/{project_id}/knowledge-graph")
def knowledge_graph(
    project_id: str,
    book_id: str | None = None,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    """Return the persisted, source-grounded concept graph for the UI."""
    repo.assert_project_owned_by(project_id, user.user_id)
    allowed = repo.allowed_book_ids(project_id)
    if book_id is not None and book_id not in allowed:
        raise HTTPException(status_code=403, detail="该资料不属于当前学习空间。")
    selected_books = [book_id] if book_id else sorted(allowed)
    concepts = [
        concept
        for selected in selected_books
        for concept in repo.concepts_for_book(selected)
    ]
    nodes = [{
        "concept_id": c.concept_id,
        "book_id": c.book_id,
        "name": c.name,
        "description": c.description,
        "chapter": c.chapter or "未分类",
        "section": c.section,
        "importance": c.importance,
        "difficulty": c.difficulty.value,
        "source": c.source,
        "source_refs": [ref.model_dump(mode="json") for ref in c.source_refs],
    } for c in concepts]

    edge_map: dict[tuple[str, str, str], dict] = {}
    for selected in selected_books:
        for relation in repo.relations_for_book(selected):
            key = (relation.source_concept_id, relation.target_concept_id, relation.relation.value)
            edge_map[key] = {
                "source": relation.source_concept_id,
                "target": relation.target_concept_id,
                "relation": relation.relation.value,
                "rationale": relation.rationale,
            }
    # Backward-compatible recovery for databases created before relation rows
    # were persisted: the Concept adjacency remains authoritative.
    for concept in concepts:
        for prerequisite in concept.prerequisites:
            key = (concept.concept_id, prerequisite, "PREREQUISITE")
            edge_map.setdefault(key, {
                "source": concept.concept_id,
                "target": prerequisite,
                "relation": "PREREQUISITE",
                "rationale": "",
            })
    return {
        "project_id": project_id,
        "source_ids": selected_books,
        "book_ids": selected_books,
        "nodes": nodes,
        "edges": list(edge_map.values()),
        "stats": {
            "concepts": len(nodes),
            "relations": len(edge_map),
            "chapters": len({n["chapter"] for n in nodes}),
        },
    }


# --- job status / SSE ------------------------------------------------------

@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    jobs: JobService = Depends(get_job_service),
) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    # Scope: the job's project must belong to the current user.
    repo.assert_project_owned_by(job.project_id, user.user_id)
    us = user_stage(job)
    return {
        "job_id": job.job_id, "source_id": job.book_id, "book_id": job.book_id,
        "state": job.state.value, "stage": job.stage.value,
        "progress": job.progress, "error": job.error,
        "user_stage": us["key"], "user_label": us["label"],
        "attempt": job.attempt,
    }


@router.get("/jobs/{job_id}/events")
def job_events(
    job_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    jobs: JobService = Depends(get_job_service),
) -> StreamingResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    repo.assert_project_owned_by(job.project_id, user.user_id)
    last_event_id = request.headers.get("last-event-id")
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else None

    def stream():
        yield from jobs.sse_stream(job_id, last_event_id=after)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    jobs: JobService = Depends(get_job_service),
    worker: BackgroundWorker = Depends(get_background_worker),
) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    repo.assert_project_owned_by(job.project_id, user.user_id)
    if job.state.value in ("RUNNING", "PENDING"):
        raise AppError("JOB_IN_PROGRESS", "该资料正在处理中，请稍候。", status_code=409)
    from ...jobs.job_store import JobState
    job.state = JobState.PENDING
    job.error = ""
    jobs.update(job)
    worker.enqueue(job.job_id)
    return {"job_id": job_id, "state": "PENDING"}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    jobs: JobService = Depends(get_job_service),
) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    repo.assert_project_owned_by(job.project_id, user.user_id)
    from ...jobs.job_store import JobState
    job.state = JobState.CANCELLED
    jobs.update(job)
    return {"job_id": job_id, "state": job.state.value}


# --- PDF source / Reader ---------------------------------------------------

@router.get("/sources/{book_id}/file")
@router.get("/books/{book_id}/source.pdf", deprecated=True)
def book_source_pdf(
    book_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    upload: UploadService = Depends(get_upload_service),
) -> FileResponse:
    """Serve the original PDF for the Reader. Scope-checked via book ownership."""
    if not repo.book_accessible_by(book_id, user.user_id):
        raise HTTPException(status_code=403, detail="无权访问该资料。")
    path = upload.path_for(user.user_id, book_id)
    if not path.is_file():
        # The book may have been uploaded by another user in the same project
        # scope; fall back to the owner's directory.
        owner = _book_owner(repo, book_id)
        if owner:
            path = upload.path_for(owner, book_id)
    if not path.is_file():
        # P1-09: the demo corpus has no distributable PDF. Surface a clear
        # message rather than a bare 404 so the Reader can show something
        # meaningful instead of a cryptic load error.
        if book_id == "demo_java_core":
            raise HTTPException(
                status_code=404,
                detail="旧版示例资料不提供可翻阅的原文件，请在引用中查看对应文字片段。",
            )
        raise HTTPException(status_code=404, detail="资料文件未找到。")
    return FileResponse(str(path), media_type="application/pdf",
                        filename=f"{book_id}.pdf")


# --- helpers ---------------------------------------------------------------

def _strip_ext(filename: str) -> str:
    import os
    return os.path.splitext(filename)[0] or "学习资料"


def _find_book_by_hash(repo: Repository, user_id: str, source_hash: str) -> Book | None:
    return repo.find_source_by_hash(user_id, source_hash)


def _get_book(repo: Repository, book_id: str) -> Book | None:
    return repo.get_source(book_id)


def _book_owner(repo: Repository, book_id: str) -> str | None:
    source = repo.get_source(book_id)
    return source.owner_user_id if source else None


def _graph_needs_rebuild(repo: Repository, book_id: str) -> bool:
    """Detect graphs created by the old 'Java skeleton for every book' bug."""
    concepts = repo.concepts_for_book(book_id)
    if not concepts:
        return True
    return book_id != "demo_java_core" and any(c.source == "GOLD" for c in concepts)
