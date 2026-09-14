"""API route package — the /api/* product surface (PRODUCTIZATION §8.1)."""

from __future__ import annotations

from .auth import router as auth_router
from .books import router as books_router
from .conversations import router as conversations_router
from .learning import router as learning_router
from .projects import router as projects_router
from .runs import router as runs_router
from .tasks import router as tasks_router

__all__ = [
    "auth_router",
    "projects_router",
    "conversations_router",
    "runs_router",
    "books_router",
    "tasks_router",
    "learning_router",
]
