"""Learning-space routes — the /api/projects compatibility surface.

Users create spaces, attach learning sources, and read their learning summary.
All project IDs are server-generated; the learner is taken from the session
cookie, never the request body.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...domain.enums import BookRole, EvidenceType, UIPreset
from ...domain.models import Book, LearningProject, ProjectBook, ReviewPolicy, User
from ...services.learner_state_view import project_state_views
from ...storage.protocols import Repository
from ..dependencies import get_current_user, get_repo
from ..errors import AppError

router = APIRouter(prefix="/api", tags=["projects"])


class CreateProjectBody(BaseModel):
    name: str = ""
    goal: str = ""
    learning_scope: str = ""
    deadline: str = ""
    current_plan: str = ""
    default_mode: UIPreset = UIPreset.QUIET_READING


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return {"user_id": user.user_id, "display_name": user.display_name}


@router.get("/projects")
def list_projects(
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> list[dict]:
    return [{
        "project_id": project.project_id,
        "name": project.name,
        "goal": project.goal,
        "learning_scope": project.learning_scope,
        "deadline": project.deadline,
        "current_plan": project.current_plan,
        "last_source_id": project.last_source_id,
        "last_source_page": project.last_source_page,
        "updated_at": project.updated_at.isoformat(),
        "last_activity_at": project.last_activity_at.isoformat(),
        "source_count": len(repo.allowed_book_ids(project.project_id)),
        "default_mode": project.default_mode.value,
    } for project in repo.projects_for_user(user.user_id)]


@router.post("/projects")
def create_project(
    body: CreateProjectBody,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    project_id = f"proj_{uuid.uuid4().hex[:12]}"
    name = body.name or "我的学习空间"
    try:
        repo.create_project(LearningProject(
            project_id=project_id, learner_id=user.user_id,
            name=name, goal=body.goal, learning_scope=body.learning_scope,
            deadline=body.deadline, current_plan=body.current_plan,
            default_mode=body.default_mode,
        ))
    except Exception:
        raise AppError(
            "PROJECT_CREATE_FAILED",
            "学习空间暂时没有创建成功，请检查名称后重试。",
            status_code=400,
            can_retry=True,
            action="RETRY",
        )
    return {
        "project_id": project_id, "name": name, "goal": body.goal,
        "learning_scope": body.learning_scope, "deadline": body.deadline,
        "current_plan": body.current_plan,
        "last_source_id": "", "last_source_page": 1,
        "default_mode": body.default_mode.value,
    }


@router.get("/projects/{project_id}")
def get_project(
    project_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    proj = repo.assert_project_owned_by(project_id, user.user_id)
    return {
        "project_id": proj.project_id, "name": proj.name, "goal": proj.goal,
        "learning_scope": proj.learning_scope, "deadline": proj.deadline,
        "current_plan": proj.current_plan,
        "last_source_id": proj.last_source_id,
        "last_source_page": proj.last_source_page,
        "updated_at": proj.updated_at.isoformat(),
        "last_activity_at": proj.last_activity_at.isoformat(),
        "source_count": len(repo.allowed_book_ids(project_id)),
        "default_mode": proj.default_mode.value, "learner_id": user.user_id,
    }


class UpdateProjectBody(BaseModel):
    name: str | None = None
    goal: str | None = None
    learning_scope: str | None = None
    deadline: str | None = None
    current_plan: str | None = None
    last_source_id: str | None = None
    last_source_page: int | None = None
    default_mode: UIPreset | None = None


@router.patch("/projects/{project_id}")
def update_project(
    project_id: str,
    body: UpdateProjectBody,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    """Update a project's name, goal, or default mode (PRODUCTIZATION §5.12,
    M6 mode switching). The mode is stored on the project and read by the
    orchestrator/decision path on subsequent turns."""
    proj = repo.assert_project_owned_by(project_id, user.user_id)
    repo.update_project(
        project_id=project_id,
        name=body.name, goal=body.goal, learning_scope=body.learning_scope,
        deadline=body.deadline, current_plan=body.current_plan,
        last_source_id=body.last_source_id,
        last_source_page=body.last_source_page,
        default_mode=body.default_mode,
    )
    return {"project_id": project_id, "updated": True}


@router.delete("/projects/{project_id}")
def delete_project(
    project_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    """Soft-delete a project (PRODUCTIZATION §5.12). A confirmation of what
    gets removed is the frontend's responsibility; this endpoint archives the
    project so it disappears from the user's list. Evidence and state are
    retained for potential restore (M6: soft delete, not hard purge)."""
    repo.assert_project_owned_by(project_id, user.user_id)
    archived = repo.archive_project(project_id)
    return {"project_id": project_id, "archived": archived}


@router.get("/projects/{project_id}/concepts/{concept_id}/record")
def concept_learning_record(
    project_id: str,
    concept_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    """Return one learner-facing concept record without task secrets."""
    repo.assert_project_owned_by(project_id, user.user_id)
    concept = next(
        (
            item for source_id in repo.allowed_book_ids(project_id)
            for item in repo.concepts_for_book(source_id)
            if item.concept_id == concept_id
        ),
        None,
    )
    if concept is None:
        raise AppError("CONCEPT_NOT_IN_SCOPE", "这个知识点不在当前学习空间中", status_code=404)
    view = next(
        item for item in project_state_views(repo, project_id, policy=ReviewPolicy())
        if item.concept_id == concept_id
    )
    source = repo.get_source(concept.book_id)
    evidence = repo.evidence_for(project_id, concept_id)
    questions = [item for item in evidence if item.evidence_type == EvidenceType.QUESTION]
    attempts = [
        item for item in evidence
        if item.evidence_type in {
            EvidenceType.VERIFY, EvidenceType.PROBE,
            EvidenceType.CHANGED_TASK, EvidenceType.CORRECTION,
        }
    ]
    refs = []
    seen_refs = set()
    for ref in concept.source_refs:
        key = (concept.book_id, ref.physical_page, ref.chunk_id)
        if key in seen_refs:
            continue
        seen_refs.add(key)
        refs.append({
            "source_id": concept.book_id,
            "source_title": source.title if source else "学习资料",
            "page": ref.physical_page,
            "chunk_id": ref.chunk_id,
            "label": ref.short_label(),
        })
    if not refs:
        for item in sorted(evidence, key=lambda value: value.occurred_at, reverse=True):
            for chunk_id in item.source_chunk_ids:
                chunk = repo.chunk_by_id(chunk_id)
                if chunk is None or chunk.book_id != concept.book_id:
                    continue
                key = (chunk.book_id, chunk.source_ref.physical_page, chunk.chunk_id)
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                refs.append({
                    "source_id": chunk.book_id,
                    "source_title": source.title if source else "学习资料",
                    "page": chunk.source_ref.physical_page,
                    "chunk_id": chunk.chunk_id,
                    "label": chunk.short_label(),
                })
    return {
        "concept_id": concept.concept_id,
        "name": concept.name,
        "description": concept.description,
        "chapter": concept.chapter,
        "section": concept.section,
        "status": {
            "group": view.group,
            "current_level": view.current_verified_level,
            "highest_level": view.highest_ever_level,
            "exposure": view.exposure,
        },
        "question_count": len(questions),
        "attempt_count": len(attempts),
        "source_refs": refs,
        "timeline": [
            {
                "evidence_id": item.evidence_id,
                "type": item.evidence_type.value,
                "result": item.result.value if item.result else None,
                "independent": item.independent,
                "hint_level": int(item.hint_level),
                "occurred_at": item.occurred_at.isoformat(),
                "question": (
                    item.content_summary.removeprefix("question:")
                    if item.evidence_type == EvidenceType.QUESTION else ""
                ),
            }
            for item in sorted(evidence, key=lambda value: value.occurred_at, reverse=True)
        ],
    }


@router.post("/projects/{project_id}/books/seed-demo")
def seed_demo(
    project_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    """Seed the offline Java demo corpus used by compatibility tests."""
    repo.assert_project_owned_by(project_id, user.user_id)
    from ...agents.demo_corpus import DemoCorpus
    corp = DemoCorpus()
    repo.add_book(Book(book_id=corp.book_id, owner_user_id=user.user_id,
                       source_hash="demo", title="Java Core (demo)"))
    try:
        repo.link_book(ProjectBook(project_id=project_id, book_id=corp.book_id, role=BookRole.PRIMARY))
    except Exception:
        pass  # already linked
    corp.seed_concepts_into(repo)
    corp.seed_chunks_into(repo)
    # P1-09: persist the demo chunks to disk so a restart (new process / empty
    # in-memory retriever) can rebuild the retriever from chunks.json and the
    # demo keeps answering questions without re-seeding. The product upload
    # path already does this; the demo path previously kept chunks in memory only.
    from ...config import get_settings
    from pathlib import Path
    import json
    settings = get_settings()
    chunks_path = Path(settings.data_dir) / "indexes" / corp.book_id / "chunks.json"
    chunks_path.parent.mkdir(parents=True, exist_ok=True)
    chunks_path.write_text(
        json.dumps([c.model_dump(mode="json") for c in corp.chunks], ensure_ascii=False),
        encoding="utf-8",
    )
    # Index demo chunks into the project retriever (offline).
    from ...retrieval.bm25 import BM25Index
    from ...retrieval.vector import VectorStore
    from ...retrieval.fusion import HybridRetriever
    from ...llm.router import ModelRouter, RouterConfig
    ret = repo.get_retriever(project_id) or HybridRetriever(
        BM25Index(), VectorStore(), ModelRouter(RouterConfig(live=False)), rerank_enabled=False,
    )
    ret.index_chunks(corp.chunks)
    repo.set_retriever(project_id, ret)
    return {"book_id": corp.book_id, "concepts": len(corp.concepts), "chunks": len(corp.chunks)}


@router.get("/projects/{project_id}/learning-summary")
def learning_summary(
    project_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    """A compact learning-state summary for the sidebar (reuses the existing
    read-side projection)."""
    repo.assert_project_owned_by(project_id, user.user_id)
    views = project_state_views(repo, project_id, policy=ReviewPolicy())
    groups: dict[str, int] = {}
    concept_rows: list[dict] = []
    questioned_count = 0
    for v in views:
        groups[v.group] = groups.get(v.group, 0) + 1
        question_evidence = [e for e in v.evidence if e.evidence_type == "QUESTION"]
        question_count = len(question_evidence)
        if question_count:
            questioned_count += 1
        concept_rows.append({
            "concept_id": v.concept_id,
            "name": v.concept_name,
            "level": v.current_verified_level,
            "group": v.group,
            "question_count": question_count,
            "last_question_at": question_evidence[0].occurred_at if question_evidence else None,
        })
    return {
        "total_concepts": len(views),
        "groups": groups,
        "questioned_count": questioned_count,
        "concepts": concept_rows,
    }


@router.get("/projects/{project_id}/misconceptions")
def project_misconceptions(
    project_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> list[dict]:
    """Read-only view of the project's misconception hypotheses (PRODUCTIZATION
    §M4 / §5.9). Browser-safe: exposes an opaque ``item_id`` (the bug key, for
    trace/refresh correlation) plus a human-readable status label, evidence
    strength band, and remediation progress — never rubric, event_key, or
    submission ids. The frontend renders "可能的理解偏差" with the status; it does
    not show the raw item_id to users (§2.4)."""
    repo.assert_project_owned_by(project_id, user.user_id)
    status_label = {
        "SUSPECTED": "待观察", "LIKELY": "可能存在", "CONFIRMED": "已确认",
        "REMEDIATING": "纠正中", "VERIFYING": "复验中", "RESOLVED": "已纠正",
        "RELAPSED": "可能又出现", "DISMISSED": "已排除",
    }
    return [
        {
            "item_id": m.bug_id,
            "status": m.status.value,
            "status_label": status_label.get(m.status.value, m.status.value),
            "evidence_band": m.confidence_band.value,
            "evidence_score": m.evidence_score,
            "changed_task_pass_count": m.changed_task_pass_count,
        }
        for m in repo.all_misconceptions(project_id)
    ]
