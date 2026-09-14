"""Auth / session routes — bootstrap anonymous local users (PRODUCTIZATION §11.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from ..dependencies import get_identity_service
from ...services.identity_service import COOKIE_MAX_AGE, COOKIE_NAME, IdentityService

router = APIRouter(prefix="/api/session", tags=["session"])


@router.post("/bootstrap")
def bootstrap(response: Response, identity: IdentityService = Depends(get_identity_service)) -> dict:
    """Create a persistent anonymous user and set an HttpOnly cookie.

    The user never sees or types a User ID. ``learner_id`` for all subsequent
    requests is derived from this cookie on the server."""
    user = identity.bootstrap()
    response.set_cookie(
        key=COOKIE_NAME, value=identity.sign(user.user_id),
        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
    )
    return {"user_id": user.user_id, "display_name": user.display_name}
