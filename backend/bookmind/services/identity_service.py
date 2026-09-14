"""Identity service — bootstrap anonymous local users (PRODUCTIZATION §11.1).

The single-machine / competition build never asks for a User ID. On first visit
the frontend calls ``POST /api/session/bootstrap``; this service creates a
persistent anonymous user, sets an HttpOnly SameSite cookie, and returns the
display name. ``learner_id`` is always derived server-side from the cookie,
never accepted from the request body (PRODUCTIZATION §11.2).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from pathlib import Path

from ..domain.models import User
from ..storage.protocols import Repository

COOKIE_NAME = "bookmind_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


class IdentityService:
    def __init__(self, repo: Repository, secret: bytes) -> None:
        self.repo = repo
        self.secret = secret

    def bootstrap(self) -> User:
        """Create a fresh anonymous user. Returns the new user."""
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        user = User(user_id=user_id, display_name="学习者")
        self.repo.add_user(user)
        return user

    def get(self, user_id: str) -> User | None:
        """Look up a user by id (works for both repository impls)."""
        return self.repo.get_user(user_id)

    def sign(self, user_id: str) -> str:
        digest = hmac.new(self.secret, user_id.encode("utf-8"), hashlib.sha256).digest()
        signature = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return f"{user_id}.{signature}"

    def verify(self, token: str) -> str | None:
        try:
            user_id, signature = token.rsplit(".", 1)
        except ValueError:
            return None
        expected = self.sign(user_id).rsplit(".", 1)[1]
        return user_id if hmac.compare_digest(signature, expected) else None


def load_session_secret(data_dir: str, configured: str = "") -> bytes:
    """Load a stable local signing key without committing it to the repo."""
    if configured:
        return configured.encode("utf-8")
    path = Path(data_dir) / ".session-secret"
    try:
        value = path.read_text("utf-8").strip()
        if value:
            return value.encode("ascii")
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(value)
    except FileExistsError:
        value = path.read_text("utf-8").strip()
    return value.encode("ascii")
