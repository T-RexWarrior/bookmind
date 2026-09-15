"""Single configuration entry point — PRODUCTIZATION §14.3.

All runtime knobs (database, data dir, host/port, CORS, model gateway) are read
here from environment variables with safe defaults. Nothing else in the codebase
should call ``os.environ.get`` directly for these settings; import ``Settings``
instead. The Docker Compose file already sets ``DATABASE_URL``; we accept both
``BOOKMIND_DATABASE_URL`` and the conventional ``DATABASE_URL`` (the former wins).

A ``.env`` file next to the project root is auto-loaded: its variables are
parsed and injected into ``os.environ`` (without overriding anything already set
in the real environment). This is what lets the LLM API key — read from the
env var named by ``BOOKMIND_LLM_API_KEY_ENV`` (default ``USTC_LLM_API_KEY``),
which has no ``BOOKMIND_`` prefix and so is not a Settings field — be configured
by dropping a line into ``.env`` instead of exporting it in every shell. The
same path picks up ``DATABASE_URL`` for Docker / single-machine parity. No
``python-dotenv`` dependency: a tiny parser handles the subset we need.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DOTENV_PATH = _PROJECT_ROOT / ".env"


def _load_dotenv(path: str | Path = _DOTENV_PATH) -> None:
    """Load variables from a ``.env`` file into ``os.environ`` (non-overriding).

    A real environment variable always wins over the file, so deploy / Docker
    env vars are not clobbered. Handles ``KEY=VALUE``, surrounding quotes, and
    ``#`` comments; ignores blank lines. Intentionally minimal — not a full
    dotenv implementation, just enough for config keys and a secret.
    """
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key or key.startswith("#"):
                    continue
                value = value.strip()
                # Strip a trailing inline comment that is outside quotes.
                if value and not value[0] in ("'", '"'):
                    hash_idx = value.find(" #")
                    if hash_idx != -1:
                        value = value[:hash_idx].strip()
                # Unwrap matching surrounding quotes.
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                os.environ.setdefault(key, value)
    except OSError:
        # A unreadable .env is non-fatal — fall back to real env vars only.
        pass


# Load once at import so the env-var-named API key is available to Settings
# and to anything that reads os.environ directly (e.g. the LLM router). Skipped
# when BOOKMIND_NO_DOTENV is set, so a developer's local .env (possibly holding
# a real API key) never leaks into the test suite and turns it live. Explicit
# _load_dotenv(path) calls are never gated — tests use them on temp files.
if not os.environ.get("BOOKMIND_NO_DOTENV"):
    _load_dotenv(_DOTENV_PATH)
    # Keep the historical cwd-based lookup as a non-overriding compatibility
    # path for isolated test/demo workspaces.  The project root is loaded first
    # and real environment variables always remain authoritative.
    cwd_dotenv = Path.cwd() / ".env"
    if cwd_dotenv.resolve() != _DOTENV_PATH.resolve():
        _load_dotenv(cwd_dotenv)


class Settings(BaseSettings):
    """Application settings loaded from env / .env.

    Defaults match the single-machine SQLite path so the project runs with zero
    configuration. Setting ``BOOKMIND_DATABASE_URL`` (or ``DATABASE_URL``) to a
    PostgreSQL URL switches to the standard deployment path.
    """

    model_config = SettingsConfigDict(
        env_file=str(_DOTENV_PATH), env_prefix="BOOKMIND_", extra="ignore",
        case_sensitive=False,
    )

    env: Literal["development", "production", "test"] = "development"

    # Persistence. ``sqlite:///./data/bookmind.db`` is the default single-machine
    # path; ``postgresql://...`` is the standard Docker path.
    database_url: str = "sqlite:///./data/bookmind.db"
    data_dir: str = "./data"

    # Real sample source shown on the welcome page. Relative paths resolve
    # from the BookMind project root, not from the caller's current directory.
    # The bundled/local default points to the file supplied beside BookMind.
    sample_book_path: str = "../dsacpp-3rd-edn.pdf"
    sample_book_title: str = "数据结构（C++语言版）第三版"

    # Upload limit (MB) for source PDFs (PRODUCTIZATION §11.3 size limit).
    max_upload_mb: int = 100

    # Network.
    host: str = "127.0.0.1"
    port: int = 18765
    cors_origins: str = "http://127.0.0.1:18765"
    session_secret: str = ""

    # Model gateway (USTC OpenAI-compatible). The API key is *not* read here so
    # that it never accidentally appears in a Settings dump; the router reads it
    # directly from the env var name below.
    llm_base_url: str = "https://api.llm.ustc.edu.cn/v1"
    llm_api_key_env: str = "USTC_LLM_API_KEY"

    # The router's chat / embedding / rerank model names can be overridden.
    chat_model: str = ""
    embedding_model: str = ""
    rerank_model: str = ""

    @field_validator("database_url", mode="before")
    @classmethod
    def _accept_conventional_database_url(cls, v: str | None) -> str:
        """Docker Compose sets ``DATABASE_URL`` (no BOOKMIND_ prefix). If the
        BOOKMIND_-prefixed var is unset, fall back to the conventional one so the
        compose file works without changes."""
        if v:
            return v
        conv = os.environ.get("DATABASE_URL")
        return conv or "sqlite:///./data/bookmind.db"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_in_memory(self) -> bool:
        return self.database_url == "memory://"

    def llm_api_key(self) -> str | None:
        """Read the LLM API key by env-var name. Never logged."""
        return os.environ.get(self.llm_api_key_env)

    @property
    def sample_book_file(self) -> Path:
        path = Path(self.sample_book_path).expanduser()
        if path.is_absolute():
            return path
        project_root = Path(__file__).resolve().parents[2]
        return (project_root / path).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
