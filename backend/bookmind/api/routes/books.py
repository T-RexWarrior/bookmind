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

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

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


class OutlineItem(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    page: int = Field(ge=1)
    page_end: int | None = Field(default=None, ge=1)
    path: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class OutlinePatch(BaseModel):
    items: list[OutlineItem] = Field(max_length=500)


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
    filename = file.filename or "upload.pdf"
    content_type = file.content_type or ""
    staged = upload.create_staging_path(user.user_id)
    digest = hashlib.sha256()
    size = 0
    header = b""
    try:
        with staged.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > upload.settings.max_upload_mb * 1024 * 1024:
                    upload.validate_metadata(size, header, filename, content_type)
                if len(header) < 5:
                    header = (header + chunk)[:5]
                digest.update(chunk)
                target.write(chunk)
        upload.validate_metadata(size, header, filename, content_type)
        return _store_and_enqueue_staged_pdf(
            project_id=project_id, staged=staged, source_hash=digest.hexdigest(),
            filename=filename, title=title, user=user, repo=repo,
            upload=upload, jobs=jobs, worker=worker,
        )
    except Exception:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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


def _store_and_enqueue_staged_pdf(
    *, project_id: str, staged: Path, source_hash: str, filename: str,
    title: str, user: User, repo: Repository, upload: UploadService,
    jobs: JobService, worker: BackgroundWorker,
) -> dict:
    """Register a streamed upload and atomically publish its original PDF."""
    book_id = f"book_{hashlib.md5((user.user_id + '|' + source_hash).encode()).hexdigest()[:12]}"
    book_title = title or _strip_ext(filename)
    link_warning = ""
    existing_book = _find_book_by_hash(repo, user.user_id, source_hash)
    reused = existing_book is not None
    if existing_book is not None:
        book_id = existing_book.book_id
        staged.unlink(missing_ok=True)
        if book_id not in repo.allowed_book_ids(project_id):
            link_warning = _link_uploaded_book(repo, project_id, book_id)
        prior = jobs.job_for_book(book_id)
        if prior and prior.state.value == "SUCCEEDED" and not _graph_needs_rebuild(repo, book_id):
            scoped = jobs.job_for_book(book_id, project_id)
            if scoped is None:
                from ...jobs.job_store import JobStage, JobState
                scoped = jobs.create(
                    project_id=project_id, book_id=book_id,
                    source_hash=source_hash, filename=filename,
                )
                scoped.stage, scoped.progress, scoped.state = JobStage.DONE, 1.0, JobState.SUCCEEDED
                jobs.update(scoped)
            return {"source_id": book_id, "book_id": book_id,
                    "job_id": scoped.job_id, "reused": True,
                    "warning": link_warning or None}
    else:
        upload.adopt_staged(staged, user.user_id, book_id)
        repo.add_book(Book(
            book_id=book_id, owner_user_id=user.user_id, source_hash=source_hash,
            title=book_title, source_type="PDF", original_filename=filename,
        ))
        link_warning = _link_uploaded_book(repo, project_id, book_id)
    job = jobs.create(
        project_id=project_id, book_id=book_id,
        source_hash=source_hash, filename=filename,
    )
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
            "pages_done": job.pages_done if job else 0,
            "pages_total": job.pages_total if job else (book.page_count if book else 0),
            "parser_mode": job.parser_mode if job else "",
            "quality_summary": job.quality_summary if job else {},
            "warnings": job.warnings if job else [],
            "checkpoint_stage": job.checkpoint_stage if job else "",
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
        "pages_done": job.pages_done, "pages_total": job.pages_total,
        "parser_mode": job.parser_mode,
        "quality_summary": job.quality_summary,
        "warnings": job.warnings,
        "checkpoint_stage": job.checkpoint_stage,
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


@router.get("/sources/{book_id}/outline")
def source_outline(
    book_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    if not repo.book_accessible_by(book_id, user.user_id):
        raise HTTPException(status_code=403, detail="无权访问该资料。")
    book = _get_book(repo, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="资料不存在。")
    return {"source_id": book_id, "items": book.outline, "parser_version": book.parser_version}


@router.patch("/sources/{book_id}/outline")
def update_source_outline(
    book_id: str,
    body: OutlinePatch,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    if not repo.book_accessible_by(book_id, user.user_id):
        raise HTTPException(status_code=403, detail="无权修改该资料。")
    items = []
    for item_model in body.items:
        item = item_model.model_dump(exclude_none=True)
        path = [part.strip() for part in item.get("path", []) if part.strip()]
        item["path"] = [*path[:-1], item["title"]] if path else [item["title"]]
        if item.get("page_end") is not None and item["page_end"] < item["page"]:
            raise AppError("OUTLINE_PAGE_RANGE", "目录结束页不能早于开始页。", status_code=422)
        items.append(item)
    for previous, current in zip(items, items[1:]):
        if current["page"] < previous["page"]:
            raise AppError("OUTLINE_PAGE_ORDER", "目录页码必须从前向后递增。", status_code=422)
    # A manual outline correction changes only section ownership. Keep OCR and
    # chunk text intact, publish the revised metadata/index atomically, and
    # then switch the live repository to the new chunk objects.
    chunks = repo.chunks_for_book(book_id)
    revised_chunks = _apply_outline_to_chunks(chunks, items)
    if revised_chunks:
        from ...retrieval.persistent_index import load_vectors, publish_index
        from ...config import get_settings

        index_root = Path(get_settings().data_dir) / "indexes" / book_id
        vector_ids, matrix, embedding_model = load_vectors(index_root)
        vector_by_id = {
            chunk_id: matrix[index].tolist()
            for index, chunk_id in enumerate(vector_ids)
        } if matrix is not None else {}
        vectors = [vector_by_id.get(chunk.chunk_id) for chunk in revised_chunks]
        publish_index(
            index_root,
            revised_chunks,
            vectors=None if any(vector is None for vector in vectors) else vectors,
            embedding_model=embedding_model,
        )
        repo.replace_chunks(book_id, revised_chunks)
        # BM25 terms and dense vectors are unchanged; only the location metadata
        # referenced by retrieval hits needs to point at the revised chunks.
        for project in repo.projects_for_user(user.user_id):
            if book_id not in repo.allowed_book_ids(project.project_id):
                continue
            retriever = repo.get_retriever(project.project_id)
            if retriever is not None:
                for chunk in revised_chunks:
                    retriever.chunks[chunk.chunk_id] = chunk
    repo.update_source_metadata(book_id, section_count=len(items), outline=items)
    return {
        "source_id": book_id, "items": items,
        "message": "目录已保存，章节归属和检索索引已更新；没有重新执行 OCR。",
    }


@router.get("/sources/{book_id}/quality")
def source_quality(
    book_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    jobs: JobService = Depends(get_job_service),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    if not repo.book_accessible_by(book_id, user.user_id):
        raise HTTPException(status_code=403, detail="无权访问该资料。")
    job = jobs.job_for_book(book_id)
    book = _get_book(repo, book_id)
    document = _latest_parsed_document(book, settings) if book else None
    page_quality = [{
        "page": page.physical_page, "printed_page": page.printed_page,
        "parser": page.parser_name, "quality_score": page.quality_score,
        "quality_label": page.quality_label, "warning": page.warning,
    } for page in document.pages] if document else []
    return {
        "source_id": book_id,
        "summary": job.quality_summary if job else {},
        "warnings": job.warnings if job else [],
        "pages_done": job.pages_done if job else 0,
        "pages_total": job.pages_total if job else 0,
        "parser_mode": job.parser_mode if job else "",
        "pages": page_quality,
    }


@router.get("/sources/{book_id}/pages/{page_number}/text-layer")
def source_page_text_layer(
    book_id: str,
    page_number: int,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    if not repo.book_accessible_by(book_id, user.user_id):
        raise HTTPException(status_code=403, detail="无权访问该资料。")
    book = _get_book(repo, book_id)
    document = _latest_parsed_document(book, settings) if book else None
    if document is None:
        return {"source_id": book_id, "page": page_number, "ready": False, "blocks": []}
    page = next((item for item in document.pages if item.physical_page == page_number), None)
    if page is None:
        return {"source_id": book_id, "page": page_number, "ready": False, "blocks": []}
    block_map = {block.block_id: block for block in document.blocks}
    return {
        "source_id": book_id, "page": page_number, "ready": True,
        "width": page.width, "height": page.height,
        "parser": page.parser_name, "printed_page": page.printed_page,
        "blocks": [{
            "block_id": block.block_id, "text": block.text, "bbox": block.bbox,
            "type": block.block_type, "confidence": block.confidence,
        } for block_id in page.block_ids
          if (block := block_map.get(block_id)) is not None and block.text and block.bbox],
    }


@router.get("/sources/{book_id}/search")
def search_source_text(
    book_id: str,
    q: str = Query(min_length=1, max_length=200),
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    if not repo.book_accessible_by(book_id, user.user_id):
        raise HTTPException(status_code=403, detail="无权访问该资料。")
    book = _get_book(repo, book_id)
    document = _latest_parsed_document(book, settings) if book else None
    query = q.casefold().strip()
    results: list[dict] = []
    if document and query:
        for block in document.blocks:
            folded = block.text.casefold()
            position = folded.find(query)
            if position < 0:
                continue
            start = max(0, position - 45)
            end = min(len(block.text), position + len(q) + 80)
            results.append({
                "page": block.physical_page, "printed_page": block.printed_page,
                "section_path": list(block.section_path),
                "snippet": block.text[start:end], "block_id": block.block_id,
            })
            if len(results) >= 100:
                break
    return {"source_id": book_id, "query": q, "results": results}


@router.post("/sources/{book_id}/reparse", status_code=202)
def reparse_source(
    book_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    jobs: JobService = Depends(get_job_service),
    worker: BackgroundWorker = Depends(get_background_worker),
) -> dict:
    if not repo.book_accessible_by(book_id, user.user_id):
        raise HTTPException(status_code=403, detail="无权重新处理该资料。")
    book = _get_book(repo, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="资料不存在。")
    project_ids = [
        project.project_id for project in repo.projects_for_user(user.user_id)
        if book_id in repo.allowed_book_ids(project.project_id)
    ]
    if not project_ids:
        raise HTTPException(status_code=409, detail="资料未关联到学习空间。")
    job = jobs.create(
        project_id=project_ids[0], book_id=book_id,
        source_hash=book.source_hash, filename=book.original_filename or "source.pdf",
    )
    job.force_reparse = True
    job.checkpoint_stage = "等待强制重新解析"
    jobs.update(job)
    worker.enqueue(job.job_id)
    return {"source_id": book_id, "job_id": job.job_id, "state": job.state.value}


# --- helpers ---------------------------------------------------------------

def _strip_ext(filename: str) -> str:
    import os
    return os.path.splitext(filename)[0] or "学习资料"


def _apply_outline_to_chunks(chunks, items: list[dict]):
    """Return metadata-only chunk revisions for a user-corrected outline."""
    if not chunks:
        return list(chunks)
    if not items:
        return [chunk.model_copy(update={
            "section_path": ("全文",),
            "source_ref": chunk.source_ref.model_copy(update={"section_path": ("全文",)}),
        }) for chunk in chunks]
    ordered = sorted(items, key=lambda item: item["page"])
    last_page = max(
        (chunk.page_end or chunk.page_start or chunk.source_ref.physical_page)
        for chunk in chunks
    )
    ranges: list[tuple[int, int, tuple[str, ...]]] = []
    for index, item in enumerate(ordered):
        start = item["page"]
        inferred_end = ordered[index + 1]["page"] - 1 if index + 1 < len(ordered) else last_page
        end = item.get("page_end") or inferred_end
        ranges.append((start, max(start, end), tuple(item.get("path") or [item["title"]])))

    revised = []
    for chunk in chunks:
        page = chunk.page_start or chunk.source_ref.physical_page
        path = next(
            (section_path for start, end, section_path in reversed(ranges) if start <= page <= end),
            chunk.section_path,
        )
        revised.append(chunk.model_copy(update={
            "section_path": path,
            "source_ref": chunk.source_ref.model_copy(update={"section_path": path}),
        }))
    return revised


def _find_book_by_hash(repo: Repository, user_id: str, source_hash: str) -> Book | None:
    return repo.find_source_by_hash(user_id, source_hash)


def _get_book(repo: Repository, book_id: str) -> Book | None:
    return repo.get_source(book_id)


def _latest_parsed_document(book: Book | None, settings: Settings):
    if book is None:
        return None
    root = Path(settings.data_dir) / "parsed" / book.source_hash
    candidates = list(root.glob("*/*/document.json")) + list(root.glob("*/document.json"))
    path = max(candidates, key=lambda item: item.stat().st_mtime, default=None)
    if path is None:
        return None
    try:
        from ...retrieval.parsed_document import ParsedDocument
        return ParsedDocument.model_validate_json(path.read_text("utf-8"))
    except Exception:
        return None


def _book_owner(repo: Repository, book_id: str) -> str | None:
    source = repo.get_source(book_id)
    return source.owner_user_id if source else None


def _graph_needs_rebuild(repo: Repository, book_id: str) -> bool:
    """Detect graphs created by the old 'Java skeleton for every book' bug."""
    concepts = repo.concepts_for_book(book_id)
    if not concepts:
        return True
    return book_id != "demo_java_core" and any(c.source == "GOLD" for c in concepts)
