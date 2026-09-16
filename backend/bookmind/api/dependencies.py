"""FastAPI dependencies — repo, current user, services (PRODUCTIZATION §8.1).

All /api/* routes depend on these. ``current_user`` is derived from the session
cookie, never from a request-body field (PRODUCTIZATION §11.2 scope rule).
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from ..config import Settings, get_settings
from ..domain.models import User
from ..llm.router import ModelRouter, RouterConfig
from ..retrieval.parsers import AdaptivePdfParser, PlainPdfFallback, PpStructureParser
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
from ..services.conversation_worker import ConversationWorker
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


def get_router(request: Request) -> ModelRouter:
    """Build a ModelRouter from the live Settings (P1-13).

    A factory translates Settings into a RouterConfig so the official DeepSeek
    endpoint, model, and file-based credential apply consistently to the API and
    both background workers. ``live`` is True only when a key is available.
    """
    shared = getattr(request.app.state, "model_router", None)
    if shared is None:
        settings = get_settings()
        live = bool(settings.llm_api_key())
        shared = ModelRouter(_router_config_from_settings(settings, live=live))
        request.app.state.model_router = shared
    return shared


def _router_config_from_settings(settings: Settings, *, live: bool) -> RouterConfig:
    """Build the single-provider DeepSeek configuration used by the product.

    DeepSeek currently exposes chat/vision but no official embedding or rerank
    endpoint. Dense retrieval is therefore disabled; the small BM25 shortlist
    is reranked through DeepSeek's JSON-capable chat API.
    """
    from ..llm.router import ModelConfig

    chat_model = settings.chat_model or "deepseek-flash"
    chat_primary = ModelConfig(
        chat_model, "chat",
        base_url=settings.llm_base_url, timeout=45.0, retries=0,
        max_tokens=4096, supports_json_mode=True, thinking_mode="disabled",
    )
    chat_fallbacks: tuple[ModelConfig, ...] = ()
    embedding = ModelConfig(
        "disabled", "embedding", base_url=settings.llm_base_url,
        timeout=1.0, retries=0,
    )
    reranker = ModelConfig(
        settings.rerank_model or chat_model, "chat_rerank",
        base_url=settings.llm_base_url, timeout=45.0, retries=0,
        max_tokens=4096, supports_json_mode=True, thinking_mode="disabled",
    )
    return RouterConfig(
        chat_primary=chat_primary,
        chat_fallbacks=chat_fallbacks,
        embedding=embedding,
        reranker=reranker,
        api_key_env=settings.llm_api_key_env,
        api_key_file=settings.llm_api_key_file,
        live=live,
        total_chat_timeout=settings.qa_deadline_seconds,
        embedding_enabled=False,
        rerank_via_chat=True,
        breaker_failures=5,
        breaker_cooldown=30.0,
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


def get_conversation_worker(
    request: Request, repo: Repository = Depends(get_repo),
) -> ConversationWorker:
    shared = getattr(request.app.state, "conversation_worker", None)
    if shared is None:
        shared = ConversationWorker(repo, max_workers=get_settings().chat_workers)
        request.app.state.conversation_worker = shared
    return shared


def get_upload_service() -> UploadService:
    return UploadService(get_settings())


def get_job_service(request: Request, repo: Repository = Depends(get_repo)) -> JobService:
    shared = getattr(request.app.state, "job_service", None)
    if shared is None:
        shared = JobService(repo)
        request.app.state.job_service = shared
    return shared


def get_ingestion_runner(
    repo: Repository = Depends(get_repo),
    router: ModelRouter = Depends(get_router),
    jobs: JobService = Depends(get_job_service),
) -> IngestionRunner:
    settings = get_settings()
    high_precision = None
    if settings.document_parser_url and settings.document_parser in {"auto", "ppstructure"}:
        high_precision = PpStructureParser(
            settings.document_parser_url, timeout=settings.document_parser_timeout,
        )
    return IngestionRunner(repo, router, jobs, parsers=[
        AdaptivePdfParser(high_precision=high_precision, batch_pages=settings.parse_batch_pages),
        PlainPdfFallback(),
    ])


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
