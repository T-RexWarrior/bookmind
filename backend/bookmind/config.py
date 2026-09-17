"""Single configuration entry point — PRODUCTIZATION §14.3.

All runtime knobs (database, data dir, host/port, CORS, model gateway) are read
here from environment variables with safe defaults. Nothing else in the codebase
should call ``os.environ.get`` directly for these settings; import ``Settings``
instead. The Docker Compose file already sets ``DATABASE_URL``; we accept both
``BOOKMIND_DATABASE_URL`` and the conventional ``DATABASE_URL`` (the former wins).

A ``.env`` file next to the project root is auto-loaded.  The LLM credential can
either come from the environment variable named by ``BOOKMIND_LLM_API_KEY_ENV``
or from ``BOOKMIND_LLM_API_KEY_FILE``.  File-based loading keeps a local secret
outside the source tree and out of Settings/log dumps.
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
    # Always prefer the project-root file, regardless of the directory used to
    # launch uvicorn/pytest. Keep the historical cwd lookup as a compatible,
    # non-overriding fallback for deployments that intentionally provide one.
    _load_dotenv(_DOTENV_PATH)
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
        env_file=None if os.environ.get("BOOKMIND_NO_DOTENV") else str(_DOTENV_PATH),
        env_prefix="BOOKMIND_", extra="ignore",
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
    sample_book_path: str = "dsacpp-3rd-edn.pdf"
    sample_book_title: str = "数据结构（C++语言版）第三版"

    # Upload limit (MB) for source PDFs (PRODUCTIZATION §11.3 size limit).
    max_upload_mb: int = 100

    # Adaptive document processing.  The private high-accuracy service is
    # optional; ``auto`` always retains the local CPU path.
    document_parser: Literal["auto", "local", "ppstructure"] = "auto"
    document_parser_url: str = ""
    document_parser_timeout: float = 300.0
    parse_batch_pages: int = 10
    ingestion_workers: int = 1
    chat_workers: int = 4
    qa_deadline_seconds: float = 75.0

    # Network.
    host: str = "127.0.0.1"
    port: int = 18765
    cors_origins: str = "http://127.0.0.1:18765"
    session_secret: str = ""

    # DeepSeek official OpenAI-compatible API.  Keep only a pointer to a key;
    # the credential itself is never a Settings field and is never logged.
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key_env: str = "DEEPSEEK_API_KEY"
    llm_api_key_file: str = ""

    # The router's chat / embedding / rerank model names can be overridden.
    chat_model: str = ""
    embedding_model: str = ""
    rerank_model: str = ""
    # Full prompts/responses can contain learner text and textbook excerpts.
    # Keep them off by default; this local-development switch is never meant
    # for the presentation-safe Trace export.
    trace_capture_content: bool = False

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
        """Read the API key from an environment variable or a local file.

        The file may contain a raw ``sk-...`` value or ``NAME=value``.  Only
        the value is returned; callers must never include it in diagnostics.
        """
        value = (os.environ.get(self.llm_api_key_env) or "").strip()
        if value:
            return value
        if not self.llm_api_key_file:
            return None
        try:
            raw = Path(self.llm_api_key_file).expanduser().read_text("utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        line = next((item.strip() for item in raw.splitlines()
                     if item.strip() and not item.lstrip().startswith("#")), "")
        if "=" in line:
            _, _, line = line.partition("=")
        return line.strip().strip("'\"") or None

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
