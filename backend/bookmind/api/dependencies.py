"""FastAPI dependencies — repo, current user, services (PRODUCTIZATION §8.1).

All /api/* routes depend on these. ``current_user`` is derived from the session
cookie, never from a request-body field (PRODUCTIZATION §11.2 scope rule).
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from ..config import Settings, get_settings
from ..domain.models import User
from ..llm.router import ModelRouter, RouterConfig
from ..retrieval.parsers import MinerUParser, PlainPdfFallback, PyPdfParser, RapidOcrParser
from ..agents.diagnostician import DiagnosticianAgent
from ..services.book_qa import BookQAService
from ..services.conversation_orchestrator import ConversationOrchestrator
from ..services.identity_service import COOKIE_NAME, IdentityService, load_session_secret
from ..services.run_service import RunService
from ..services.task_service import TaskService
from ..services.upload_service import UploadService
from ..services.job_service import JobService
from ..services.ingestion_runner import IngestionRunner
from ..services.background_worker import BackgroundWorker
from ..storage.protocols import Repository


def get_repo(request: Request) -> Repository:
    """The repository chosen at app creation (in app.state.repo)."""
    return request.app.state.repo


def get_settings_dep() -> Settings:
    return get_settings()


def get_identity_service(repo: Repository = Depends(get_repo)) -> IdentityService:
    settings = get_settings()
    return IdentityService(
        repo,
        load_session_secret(settings.data_dir, settings.session_secret),
    )


def get_current_user(
    request: Request,
    identity: IdentityService = Depends(get_identity_service),
) -> User:
    """Derive the user from the session cookie. Never trusts a body field."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="no session; call POST /api/session/bootstrap")
    user_id = identity.verify(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="session signature invalid; please re-bootstrap")
    user = identity.get(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="session invalid; please re-bootstrap")
    return user


def get_router() -> ModelRouter:
    """Build a ModelRouter from the live Settings (P1-13).

    The old code ignored ``BOOKMIND_CHAT_MODEL`` / ``BOOKMIND_LLM_BASE_URL`` /
    ``BOOKMIND_LLM_API_KEY_ENV`` and always used the RouterConfig defaults
    (glm-5.2-107 + the campus gateway). A factory here translates Settings into a
    RouterConfig so env overrides actually take effect across the API and the
    background worker. ``live`` is True only when an API key is present.
    """
    settings = get_settings()
    live = bool(settings.llm_api_key())
    cfg = _router_config_from_settings(settings, live=live)
    return ModelRouter(cfg)


def _router_config_from_settings(settings: Settings, *, live: bool) -> RouterConfig:
    """Translate Settings → RouterConfig, overriding model names + base URL +
    api-key env name when set. Keeps the default fallback chain otherwise."""
    from ..llm.router import ModelConfig

    # Keep the user-facing path bounded. The previous GLM-only configuration
    # regularly spent the entire 35-second request window reasoning and then
    # forced an otherwise healthy, grounded question into the offline excerpt
    # fallback. Use the configured fast chat model first and one short fallback
    # so the browser gets a useful answer within its own request deadline.
    chat_model = settings.chat_model or "qwen-chat"
    chat_primary = ModelConfig(
        chat_model, "chat",
        base_url=settings.llm_base_url, timeout=25.0, retries=0,
        max_tokens=1024, supports_json_mode=True,
    )
    fallback_name = "glm-5.3-flash" if chat_model != "glm-5.3-flash" else "qwen-chat"
    chat_fallbacks: tuple[ModelConfig, ...] = (
        ModelConfig(
            fallback_name, "chat",
            base_url=settings.llm_base_url, timeout=20.0, retries=0,
            max_tokens=1024, supports_json_mode=False,
        ),
    )
    embedding = ModelConfig(
        settings.embedding_model or "qwen3-embedding", "embedding",
        base_url=settings.llm_base_url, timeout=12.0, retries=0,
    )
    reranker = ModelConfig(
        settings.rerank_model or "qwen3-reranker", "rerank",
        base_url=settings.llm_base_url, timeout=8.0, retries=0,
    )
    return RouterConfig(
        chat_primary=chat_primary,
        chat_fallbacks=chat_fallbacks,
        embedding=embedding,
        reranker=reranker,
        api_key_env=settings.llm_api_key_env,
        live=live,
    )


def get_qa_service(
    repo: Repository = Depends(get_repo),
    router: ModelRouter = Depends(get_router),
) -> BookQAService:
    # Product ingestion is owned by IngestionRunner + the persistent JobService.
    # BookQAService handles retrieval only on this path; the old in-memory
    # worker is created lazily solely when the compatibility ``ingest`` method
    # is called.
    return BookQAService(repo, router)


def get_diagnostician(router: ModelRouter = Depends(get_router)) -> DiagnosticianAgent:
    return DiagnosticianAgent(router)


def get_task_service(
    repo: Repository = Depends(get_repo),
    router: ModelRouter = Depends(get_router),
    diagnostician: DiagnosticianAgent = Depends(get_diagnostician),
) -> TaskService:
    return TaskService(repo, router, diagnostician)


def get_orchestrator(
    repo: Repository = Depends(get_repo),
    router: ModelRouter = Depends(get_router),
    qa: BookQAService = Depends(get_qa_service),
    task_service: TaskService = Depends(get_task_service),
) -> ConversationOrchestrator:
    return ConversationOrchestrator(repo, router, qa, task_service=task_service)


def get_run_service(repo: Repository = Depends(get_repo)) -> RunService:
    return RunService(repo)


def get_upload_service() -> UploadService:
    return UploadService(get_settings())


def get_job_service(repo: Repository = Depends(get_repo)) -> JobService:
    return JobService(repo)


def get_ingestion_runner(
    repo: Repository = Depends(get_repo),
    router: ModelRouter = Depends(get_router),
    jobs: JobService = Depends(get_job_service),
) -> IngestionRunner:
    return IngestionRunner(
        repo, router, jobs,
        parsers=[MinerUParser(), PyPdfParser(), RapidOcrParser(), PlainPdfFallback()],
    )


def get_background_worker(
    request: Request,
    jobs: JobService = Depends(get_job_service),
    runner: IngestionRunner = Depends(get_ingestion_runner),
) -> BackgroundWorker:
    """The single background worker, created once and stored on app.state."""
    worker = getattr(request.app.state, "background_worker", None)
    if worker is None:
        worker = BackgroundWorker(runner, jobs)
        request.app.state.background_worker = worker
    return worker
