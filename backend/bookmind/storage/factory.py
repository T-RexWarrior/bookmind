"""Repository factory — selects the concrete implementation from configuration
(PRODUCTIZATION M1, ARCHITECTURE §9).

``DATABASE_URL`` / ``BOOKMIND_DATABASE_URL`` chooses the backend:
  * ``memory://`` or unset-in-development → InMemoryRepository (tests, ephemeral)
  * ``sqlite:///...`` → SqlRepository on a local file
  * ``postgresql://...`` → SqlRepository on the standard deployment DB
"""

from __future__ import annotations

from ..config import Settings, get_settings
from .in_memory import InMemoryRepository
from .protocols import Repository
from .sql import SqlRepository


def make_repository(settings: Settings | None = None) -> Repository:
    """Build the repository the app will use for its lifetime."""
    s = settings or get_settings()
    if s.is_in_memory or s.database_url.startswith("memory://"):
        return InMemoryRepository()
    # SQLite / PostgreSQL → SQL repository. Ensure the data dir exists for a
    # file-based SQLite DB so first-run startup doesn't fail silently.
    if s.is_sqlite and s.data_dir:
        import os
        os.makedirs(s.data_dir, exist_ok=True)
    repo = SqlRepository(s.database_url)
    # Auto-create tables and apply the small additive compatibility upgrade.
    repo.create_schema()
    return repo
