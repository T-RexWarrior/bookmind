"""Learning-state routes — recovery review plan + per-concept detail
(PRODUCTIZATION §8.2, M6).

These are the authenticated, /api-prefixed versions of the Phase 6 recovery
endpoints. The legacy ``/projects/{pid}/recovery`` surface (in app.py) bypassed
``get_current_user``; these routes scope-check via the session cookie like every
other /api route. The recovery logic itself is read-only and lives in
``services/recovery.py`` (LEARNING_MODEL §12).
"""

from __future__ import annotations

from datetime import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...domain.models import ReviewPolicy, User
from ...services.recovery import RecoveryService, project_recovery_plan
from ...storage.protocols import Repository
from ..dependencies import get_current_user, get_repo

router = APIRouter(prefix="/api", tags=["learning"])

_POLICY = ReviewPolicy()


@router.get("/projects/{project_id}/review-plan")
def review_plan(
    project_id: str,
    last_active_at: str | None = None,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    """Build the recovery recommendation (PRODUCTIZATION §5.10, LEARNING_MODEL
    §12). Read-only: scans high-value concepts for EXPIRED/UNSTABLE, ranks by
    the §11 key, and recommends a ~3-minute check or continuing. The user can
    always choose to continue directly — that choice is recorded via the
    /review-plan/start endpoint and never writes negative Evidence."""
    repo.assert_project_owned_by(project_id, user.user_id)
    laa = None
    if last_active_at:
        try:
            laa = _dt.fromisoformat(last_active_at)
        except ValueError:
            raise HTTPException(status_code=400, detail="last_active_at must be ISO 8601")
    plan = project_recovery_plan(repo, project_id, last_active_at=laa, policy=_POLICY)
    return plan.to_dict()


class ReviewStartBody(BaseModel):
    choice: str  # "recovery_check" | "continue"


@router.post("/projects/{project_id}/review-plan/start")
def review_plan_start(
    project_id: str,
    body: ReviewStartBody,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
) -> dict:
    """Record the user's recovery choice (LEARNING_MODEL §12.5: 用户选择优先).
    Returns the resulting next Action + target concept. Choosing "continue"
    never writes negative Evidence — it only records the preference."""
    repo.assert_project_owned_by(project_id, user.user_id)
    svc = RecoveryService(repo)
    plan = svc.build_plan(project_id=project_id, policy=_POLICY)
    try:
        svc.record_choice(plan, body.choice)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    action, target = svc.recommend_action(plan)
    return {
        "choice": plan.user_choice,
        "recommended_action": action.value,
        "target_concept_id": target,
        "candidates": [c.__dict__ for c in plan.candidates],
        "rationale": plan.rationale,
    }
