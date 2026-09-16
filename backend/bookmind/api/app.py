"""FastAPI application — exposes the deterministic closed loop over HTTP.

For the offline-demo phase the Diagnostician is stubbed: the API accepts a
caller-supplied AnswerJudgment (or a simple result) so the full state-write
transaction runs without a live model. A real ModelRouter-backed
Diagnostician plugs in behind the same ``submit_answer`` contract.

Routes:
  POST /users
  POST /projects
  POST /projects/{pid}/books/seed        — seed the Java skeleton + demo book
  GET  /projects/{pid}/concepts
  GET  /projects/{pid}/state
  POST /projects/{pid}/next-action
  POST /projects/{pid}/submit-answer
  GET  /projects/{pid}/misconceptions
  GET  /health
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel

from ..domain.enums import (
    Action,
    ActivityMode,
    BookRole,
    EvidenceResult,
    EvidenceType,
    InterventionPolicy,
    JudgmentStatus,
    Level,
    MisconceptionStatus,
    UIPreset,
)
from ..domain.models import (
    AnswerJudgment,
    Book,
    Concept,
    InteractionContext,
    LearningProject,
    ProjectBook,
    ReviewPolicy,
    TrustedTaskContext,
    User,
)
from ..agents.concept_skeleton import build_skeleton
from ..config import get_settings
from ..engine.decision.next_action import ConceptView, DecisionInput, decide
from ..engine.learning_engine import apply_expiry, record_exposure, start_remediation, submit_answer
from ..llm.router import ModelRouter, RouterConfig
from ..retrieval.parsers import PlainPdfFallback
from ..jobs import JobStore, IngestionWorker
from ..services import BookMappingService, BookQAService, project_state_views
from ..storage.errors import ScopeError
from ..storage.in_memory import InMemoryRepository
from ..storage.protocols import Repository
from ..services.book_mapping import MappingReport


# --- single shared repo (demo scope) -------------------------------------
_REPO: InMemoryRepository = InMemoryRepository()
_POLICY = ReviewPolicy()


def get_repo() -> InMemoryRepository:
    return _REPO


def get_qa_service() -> BookQAService:
    """Lazily build the QA service, sharing the module repo and an offline
    router by default. A live router is used when the USTC_LLM_API_KEY env var
    is set (configured via Settings)."""
    global _QA_SERVICE
    if _QA_SERVICE is None:
        settings = get_settings()
        live = bool(settings.llm_api_key())
        from .dependencies import _router_config_from_settings
        router = ModelRouter(_router_config_from_settings(settings, live=live))
        worker = IngestionWorker(JobStore(), router, parsers=_parsers())
        _QA_SERVICE = BookQAService(_REPO, router, worker=worker)
    return _QA_SERVICE


_QA_SERVICE: BookQAService | None = None
_MAPPING_SERVICE: BookMappingService | None = None
# Last mapping report per (project, book), for the undo endpoint.
_LAST_MAPPING: dict[tuple[str, str], "MappingReport"] = {}


def _parsers():
    from ..retrieval.parsers import MinerUParser, PyPdfParser, RapidOcrParser
    return [MinerUParser(), PyPdfParser(), RapidOcrParser(), PlainPdfFallback()]


def get_mapping_service() -> BookMappingService:
    """Lazily build the Book Mapping service, sharing the module repo + router."""
    global _MAPPING_SERVICE
    if _MAPPING_SERVICE is None:
        settings = get_settings()
        live = bool(settings.llm_api_key())
        from .dependencies import _router_config_from_settings
        router = ModelRouter(_router_config_from_settings(settings, live=live))
        _MAPPING_SERVICE = BookMappingService(_REPO, router)
    return _MAPPING_SERVICE


def _new_offline_retriever():
    from ..retrieval.bm25 import BM25Index
    from ..retrieval.vector import VectorStore
    from ..retrieval.fusion import HybridRetriever
    return HybridRetriever(BM25Index(), VectorStore(),
                           ModelRouter(RouterConfig(live=False)), rerank_enabled=False)


# --- request/response schemas (module-level for FastAPI introspection) ----

class CreateUser(BaseModel):
    user_id: str
    display_name: str = ""


class CreateProject(BaseModel):
    project_id: str
    learner_id: str
    name: str
    goal: str = ""
    default_mode: UIPreset = UIPreset.QUIET_READING


class SeedBook(BaseModel):
    book_id: str
    title: str = "Java Core Concepts"


class NextActionRequest(BaseModel):
    activity_mode: ActivityMode
    intervention_policy: InterventionPolicy
    ui_preset: UIPreset
    chapter_just_ended: bool = False
    key_concepts_unverified: bool = False
    has_active_reading_passage: bool = False
    user_requested_action: Action | None = None
    user_requested_concept_id: str | None = None


class SubmitAnswerRequest(BaseModel):
    task_id: str
    task_version: int = 1
    target_concept_ids: list[str]
    evidence_for_levels: list[Level]
    rubric: list[str]
    is_probe: bool = False
    is_changed_task: bool = False
    scenario_fingerprint: str | None = None
    discriminated_bug_ids: list[str] = []
    # learner judgment (offline stub: caller supplies it)
    result: EvidenceResult
    misconception_signals: list[dict[str, Any]] = []
    # interaction facts
    hints_issued: int = 0
    tools_exposed: list[str] = []
    answer_text: str = ""
    submission_id: str


class StartRemediationRequest(BaseModel):
    bug_id: str


class IngestRequest(BaseModel):
    learner_id: str
    book_id: str
    filename: str = "upload.pdf"
    title: str = ""
    content_base64: str  # raw PDF bytes, base64-encoded


class AskRequest(BaseModel):
    learner_id: str
    question: str
    top_k: int = 12
    context_budget: int = 12


class MapBookRequest(BaseModel):
    learner_id: str
    book_id: str
    graph_key: str = ""


class ExposureRequest(BaseModel):
    learner_id: str
    concept_id: str
    evidence_type: str  # READ | QUESTION | EXPLANATION
    read_coverage: float | None = None
    explicit_complete: bool = False
    source_chunk_ids: list[str] = []


# --- Phase 5: misconception closure --------------------------------------

class ProbeRequest(BaseModel):
    bug_id: str
    target_concept_ids: list[str] = []
    level: Level = Level.L2


class ChangedTaskRequest(BaseModel):
    bug_id: str
    stage: int  # 1 = near transfer, 2 = far transfer
    target_concept_ids: list[str] = []
    level: Level = Level.L3


class ClassifyProbeRequest(BaseModel):
    bug_id: str
    answer_text: str


class ValidateTaskRequest(BaseModel):
    task_id: str
    task_version: int = 1
    target_concept_ids: list[str]
    evidence_for_levels: list[Level]
    rubric: list[str]
    prompt_text: str = ""
    expected_answer: str = ""
    distractors: list[str] = []
    is_probe: bool = False
    is_changed_task: bool = False
    discriminated_bug_ids: list[str] = []
    scenario_fingerprint: str | None = None
    remediation_stage: int = 0


class RecoveryChooseRequest(BaseModel):
    choice: str  # "recovery_check" | "continue"


def create_app(repo: Repository | None = None) -> FastAPI:
    """Assemble the FastAPI app.

    ``repo`` optionally injects a repository (e.g. a SqlRepository for the
    productized /api/* path). When ``None`` the module-level ``_REPO``
    (InMemoryRepository) is used for the compatibility test surface.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Start the background ingestion worker (M3). It recovers leftover
        # RUNNING jobs on startup. Only the product (injected-repo) path wires
        # the worker; the in-memory test path skips it (tests drive the runner
        # directly) so those tests stay hermetic and fast.
        if repo is not None:
            from ..services.background_worker import BackgroundWorker
            from ..services.ingestion_runner import IngestionRunner
            from ..services.job_service import JobService
            from ..llm.router import ModelRouter
            from ..retrieval.parsers import AdaptivePdfParser, PlainPdfFallback, PpStructureParser
            from .dependencies import _router_config_from_settings
            settings = get_settings()
            jobs = JobService(repo)
            model_router = ModelRouter(_router_config_from_settings(
                settings, live=bool(settings.llm_api_key()),
            ))
            high_precision = None
            if settings.document_parser_url and settings.document_parser in {"auto", "ppstructure"}:
                high_precision = PpStructureParser(
                    settings.document_parser_url, timeout=settings.document_parser_timeout,
                )
            runner = IngestionRunner(
                repo,
                model_router,
                jobs, parsers=[
                    AdaptivePdfParser(high_precision=high_precision, batch_pages=settings.parse_batch_pages),
                    PlainPdfFallback(),
                ],
            )
            worker = BackgroundWorker(runner, jobs)
            app.state.background_worker = worker
            app.state.job_service = jobs
            app.state.ingestion_runner = runner
            app.state.model_router = model_router
            worker.start()
        yield
        # Shutdown: stop the worker thread.
        worker = getattr(app.state, "background_worker", None)
        if worker is not None:
            worker.stop()
        chat_worker = getattr(app.state, "conversation_worker", None)
        if chat_worker is not None:
            chat_worker.stop()

    app = FastAPI(title="学迹 · 资料学习空间", version="0.1.0", lifespan=lifespan)
    # The injected repo (or the module-level in-memory one in hermetic tests)
    # is the authoritative store for every /api/* route. The compatibility
    # endpoints below are mounted only when no repository is injected.
    app.state.repo = repo if repo is not None else _REPO

    # --- /api/* product routes + error handlers (PRODUCTIZATION M2) -------
    from .errors import (
        AppError, app_error_handler, http_error_handler, scope_error_handler,
        unhandled_error_handler, validation_error_handler,
    )
    from .routes import (
        auth_router, books_router, conversations_router, learning_router,
        projects_router, runs_router, tasks_router,
    )
    app.include_router(auth_router)
    app.include_router(projects_router)
    app.include_router(books_router)
    app.include_router(conversations_router)
    app.include_router(runs_router)
    app.include_router(tasks_router)
    app.include_router(learning_router)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    from fastapi.exceptions import RequestValidationError
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(ScopeError, scope_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    # CORS: development is permissive (the static frontend may be served from
    # any origin during local dev). Production must pin allowed origins
    # (PRODUCTIZATION §11.4). Configured via BOOKMIND_CORS_ORIGINS / BOOKMIND_ENV.
    from fastapi.middleware.cors import CORSMiddleware
    settings = get_settings()
    if settings.env == "production" and settings.cors_origin_list:
        origins: list[str] = settings.cors_origin_list
    else:
        origins = ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins, allow_methods=["*"], allow_headers=["*"],
    )

    # Serve the product frontend (ARCHITECTURE §13: backend 托管前端; §7.1:
    # commit the build so competition venues run with no Node toolchain).
    # The built React app lives in frontend/dist. We mount only the static
    # assets directory at /ui/assets, and serve index.html (plus legacy files)
    # via a catch-all that doubles as the SPA fallback — so a refresh on a
    # client route like /ui/projects/:id never 404s.
    import mimetypes
    import os
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    _frontend_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend")
    _dist_dir = os.path.join(_frontend_dir, "dist")
    _serve_root = _dist_dir if os.path.isdir(_dist_dir) else _frontend_dir
    _assets_dir = os.path.join(_serve_root, "assets")
    if os.path.isdir(_assets_dir):
        # Windows commonly maps ``.mjs`` to text/plain. PDF.js loads its
        # worker with dynamic import(), which browsers reject unless it is a
        # JavaScript MIME type even when the file itself returned HTTP 200.
        mimetypes.add_type("application/javascript", ".mjs")
        app.mount("/ui/assets", StaticFiles(directory=_assets_dir), name="ui-assets")

    @app.get("/ui/{full_path:path}")
    def _ui_serve(full_path: str):  # noqa: D401
        # Real files (legacy-debug.html, favicon, etc.) under the serve root.
        candidate = os.path.join(_serve_root, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        # Fall back to the legacy frontend dir for files that only exist there
        # (e.g. legacy-debug.html when serving the built dist).
        legacy_candidate = os.path.join(_frontend_dir, full_path)
        if full_path and os.path.isdir(_dist_dir) and os.path.isfile(legacy_candidate):
            return FileResponse(legacy_candidate)
        # SPA fallback: client-side routes serve index.html.
        index = os.path.join(_serve_root, "index.html")
        if os.path.isfile(index):
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="not found")

    # --- public infrastructure routes -----------------------------------

    @app.get("/")
    def root() -> dict:
        """Redirect humans to the product UI."""
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/ui/")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": "0.1.0"}

    @app.get("/ready")
    def ready() -> dict:
        """Process liveness vs. actual service readiness (PRODUCTIZATION §8.1).

        ``/health`` says the process is alive; ``/ready`` says the persistence
        and storage layer can actually serve. Reports the dependency-injected
        repo (app.state.repo): in-memory for tests, SQLite/PostgreSQL in prod.
        """
        repo = app.state.repo
        ready = {"status": "ok", "database": "in-memory"}
        if hasattr(repo, "ping"):
            try:
                repo.ping()
                # Readiness is public. Report only the backend kind, never a
                # connection URL that may contain a username or password.
                database_url = get_settings().database_url
                ready["database"] = database_url.split(":", 1)[0].split("+", 1)[0]
            except Exception as e:  # pragma: no cover - defensive
                ready["status"] = "not_ready"
                ready["database"] = f"error: {e}"
        return ready

    # Compatibility endpoints from the deterministic prototype live on an
    # isolated router.  They are mounted only by ``create_app()``'s hermetic
    # in-memory test mode; the production app exposes one authoritative
    # ``/api/*`` surface and cannot accidentally write to the module-level
    # in-memory repository.
    legacy_router = APIRouter(tags=["legacy"], include_in_schema=False)

    @legacy_router.post("/users")
    def create_user(body: CreateUser) -> dict:
        repo = get_repo()
        repo.add_user(User(user_id=body.user_id, display_name=body.display_name))
        return {"user_id": body.user_id}

    @legacy_router.post("/projects")
    def create_project(body: CreateProject) -> dict:
        repo = get_repo()
        try:
            repo.create_project(LearningProject(
                project_id=body.project_id, learner_id=body.learner_id,
                name=body.name, goal=body.goal, default_mode=body.default_mode,
            ))
        except ScopeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"project_id": body.project_id}

    @legacy_router.post("/projects/{pid}/books/seed")
    def seed_book(pid: str, body: SeedBook) -> dict:
        repo = get_repo()
        proj = repo.projects.get(pid)
        if proj is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            repo.add_book(Book(book_id=body.book_id, owner_user_id=proj.learner_id, source_hash=body.book_id, title=body.title))
            repo.link_book(ProjectBook(project_id=pid, book_id=body.book_id, role=BookRole.PRIMARY))
        except ScopeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # seed the gold concept skeleton into this book
        for c in build_skeleton(body.book_id):
            repo.add_concept(c)
        return {"book_id": body.book_id, "concepts": len(repo.concepts_for_book(body.book_id))}

    @legacy_router.get("/projects/{pid}/concepts")
    def list_concepts(pid: str) -> list[dict]:
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        out = []
        for bid in repo.allowed_book_ids(pid):
            for c in repo.concepts_for_book(bid):
                out.append({
                    "concept_id": c.concept_id, "name": c.name, "chapter": c.chapter,
                    "importance": c.importance, "difficulty": c.difficulty.value,
                    "prerequisites": c.prerequisites, "source": c.source,
                })
        return out

    @legacy_router.get("/projects/{pid}/state")
    def project_state(pid: str) -> list[dict]:
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        out = []
        for bid in repo.allowed_book_ids(pid):
            for c in repo.concepts_for_book(bid):
                s = repo.get_state(pid, c.concept_id)
                out.append({
                    "concept_id": c.concept_id,
                    "exposure": s.exposure_state.value,
                    "current_verified_level": s.current_verified_level.value,
                    "highest_ever_level": s.highest_ever_level.value,
                    "L1": s.level_record(Level.L1).status.value,
                    "L2": s.level_record(Level.L2).status.value,
                    "L3": s.level_record(Level.L3).status.value,
                    "L4": s.level_record(Level.L4).status.value,
                })
        return out

    @legacy_router.get("/projects/{pid}/misconceptions")
    def project_misconceptions(pid: str) -> list[dict]:
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        return [
            {
                "bug_id": m.bug_id, "status": m.status.value,
                "evidence_score": m.evidence_score, "confidence_band": m.confidence_band.value,
                "changed_task_pass_count": m.changed_task_pass_count,
            }
            for m in repo.all_misconceptions(pid)
        ]

    @legacy_router.post("/projects/{pid}/next-action")
    def next_action(pid: str, body: NextActionRequest) -> dict:
        repo = get_repo()
        proj = repo.projects.get(pid)
        if proj is None:
            raise HTTPException(status_code=404, detail="project not found")
        views: list[ConceptView] = []
        for bid in repo.allowed_book_ids(pid):
            for c in repo.concepts_for_book(bid):
                s = repo.get_state(pid, c.concept_id)
                views.append(ConceptView(concept=c, state=s))
        mis = repo.all_misconceptions(pid)
        inp = DecisionInput(
            activity_mode=body.activity_mode,
            intervention_policy=body.intervention_policy,
            ui_preset=body.ui_preset.value,
            concepts=views,
            misconceptions=mis,
            chapter_just_ended=body.chapter_just_ended,
            key_concepts_unverified=body.key_concepts_unverified,
            has_active_reading_passage=body.has_active_reading_passage,
            user_requested_action=body.user_requested_action,
            user_requested_concept_id=body.user_requested_concept_id,
        )
        trace = decide(inp)
        return {
            "selected_action": trace.selected_action,
            "selected_concept_id": trace.selected_concept_id,
            "selected_rule": trace.selected_rule,
            "reason": trace.reason,
            "checked_rules": trace.checked_rules,
        }

    @legacy_router.post("/projects/{pid}/submit-answer")
    def submit(pid: str, body: SubmitAnswerRequest) -> dict:
        repo = get_repo()
        proj = repo.projects.get(pid)
        if proj is None:
            raise HTTPException(status_code=404, detail="project not found")
        task = TrustedTaskContext(
            task_id=body.task_id, task_version=body.task_version,
            target_concept_ids=body.target_concept_ids,
            evidence_for_levels=body.evidence_for_levels, rubric=body.rubric,
            is_probe=body.is_probe, is_changed_task=body.is_changed_task,
            scenario_fingerprint=body.scenario_fingerprint,
            discriminated_bug_ids=body.discriminated_bug_ids,
        )
        interaction = InteractionContext(
            activity_mode=ActivityMode.READING,
            intervention_policy=InterventionPolicy.PROACTIVE,
            ui_preset=UIPreset.DEEP_LEARNING,
            hints_issued=body.hints_issued, tools_exposed=body.tools_exposed,
            answer_submitted_at=datetime.now(timezone.utc),
        )
        from ..domain.models import MisconceptionSignal
        from ..domain.enums import SignalDirection, SignalStrength
        signals = []
        for s in body.misconception_signals:
            signals.append(MisconceptionSignal(
                bug_id=s["bug_id"],
                direction=SignalDirection(s.get("direction", "FOR")),
                strength=SignalStrength(s.get("strength", "MEDIUM")),
                reason=s.get("reason", ""),
            ))
        judgment = AnswerJudgment(
            judgment_status=JudgmentStatus.DECIDED, result=body.result,
            misconception_signals=signals,
        )
        book_id = next(iter(repo.allowed_book_ids(pid)))
        try:
            res = submit_answer(
                repo, learner_id=proj.learner_id, project_id=pid, task=task,
                interaction=interaction, judgment=judgment, answer_text=body.answer_text,
                policy=_POLICY, submission_id=body.submission_id,
                evidence_id=f"e{uuid.uuid4().hex[:8]}", source_book_id=book_id,
            )
        except ScopeError as e:
            raise HTTPException(status_code=403, detail=str(e))
        return {
            "written": res.written,
            "evidence_id": res.evidence_id,
            "verified_levels": [l.value for l in res.verified_levels],
            "mastery_transitions": [t.model_dump(mode="json") for t in res.mastery_transitions],
            "misconception_transitions": [t.model_dump(mode="json") for t in res.misconception_transitions],
            "needs_review": res.needs_review,
            "gate_blocks": res.gate_blocks,
            "reason": res.reason,
        }

    @legacy_router.post("/projects/{pid}/start-remediation")
    def start_remed(pid: str, body: StartRemediationRequest) -> dict:
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        mis = start_remediation(repo, project_id=pid, bug_id=body.bug_id)
        if mis is None:
            raise HTTPException(status_code=404, detail="bug not found")
        return {"bug_id": mis.bug_id, "status": mis.status.value}

    # --- Phase 2: ingestion & textbook Q&A --------------------------------

    @legacy_router.post("/projects/{pid}/seed-demo")
    def seed_demo(pid: str) -> dict:
        """Seed the offline Java demo corpus (no upload/network needed)."""
        repo = get_repo()
        proj = repo.projects.get(pid)
        if proj is None:
            raise HTTPException(status_code=404, detail="project not found")
        from ..agents.demo_corpus import DemoCorpus
        corp = DemoCorpus()
        repo.add_book(Book(book_id=corp.book_id, owner_user_id=proj.learner_id,
                           source_hash="demo", title="Java Core (demo)"))
        try:
            repo.link_book(ProjectBook(project_id=pid, book_id=corp.book_id, role=BookRole.PRIMARY))
        except ScopeError:
            pass
        corp.seed_concepts_into(repo)
        corp.seed_chunks_into(repo)
        # Index the demo chunks into the project's retriever (offline router).
        svc = get_qa_service()
        ret = repo.get_retriever(pid) or _new_offline_retriever()
        from ..retrieval.chunk import DocumentChunk
        ret.index_chunks(corp.chunks)
        repo.set_retriever(pid, ret)
        return {
            "book_id": corp.book_id, "concepts": len(corp.concepts),
            "chunks": len(corp.chunks),
        }

    @legacy_router.post("/projects/{pid}/ingest")
    def ingest(pid: str, body: IngestRequest) -> dict:
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            source = base64.b64decode(body.content_base64)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"bad base64: {e}")
        svc = get_qa_service()
        try:
            res = svc.ingest(project_id=pid, learner_id=body.learner_id, book_id=body.book_id,
                             source=source, filename=body.filename, title=body.title)
        except ScopeError as e:
            raise HTTPException(status_code=403, detail=str(e))
        return res

    @legacy_router.post("/projects/{pid}/ask")
    def ask(pid: str, body: AskRequest) -> dict:
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        svc = get_qa_service()
        try:
            ans = svc.ask(project_id=pid, learner_id=body.learner_id, question=body.question,
                          top_k=body.top_k, context_budget=body.context_budget)
        except ScopeError as e:
            raise HTTPException(status_code=403, detail=str(e))
        return {
            "answer_text": ans.answer_text, "citations": ans.citations,
            "grounded": ans.grounded, "chunk_ids": ans.chunk_ids, "reason": ans.reason,
        }

    @legacy_router.get("/projects/{pid}/chunks")
    def list_chunks(pid: str) -> list[dict]:
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        out = []
        for c in repo.chunks_for_project(pid):
            out.append({
                "chunk_id": c.chunk_id, "book_id": c.book_id,
                "page": c.source_ref.physical_page,
                "section_path": list(c.section_path),
                "content": c.content[:200],
            })
        return out

    # --- Phase 3: Book Mapping -------------------------------------------

    @legacy_router.post("/projects/{pid}/map-book")
    def map_book(pid: str, body: MapBookRequest) -> dict:
        """Build the concept graph for a book: per-section Book Mapper proposals
        → deterministic merge/cycle-removal/gold-protection → persist."""
        repo = get_repo()
        proj = repo.projects.get(pid)
        if proj is None:
            raise HTTPException(status_code=404, detail="project not found")
        if body.book_id not in repo.allowed_book_ids(pid):
            raise HTTPException(status_code=403, detail=f"book {body.book_id} not in project scope")
        svc = get_mapping_service()
        # Use the book's parsed document if available (none for the offline
        # seed-demo path, which maps from chunks alone).
        chunks = repo.chunks_for_book(body.book_id)
        try:
            report = svc.map_book(
                project_id=pid, learner_id=body.learner_id, book_id=body.book_id,
                parsed_document=None, chunks=chunks, graph_key=body.graph_key,
            )
        except ScopeError as e:
            raise HTTPException(status_code=403, detail=str(e))
        _LAST_MAPPING[(pid, body.book_id)] = report
        return {
            "book_id": report.book_id, "total_concepts": report.total_concepts,
            "gold_concepts": report.gold_concepts, "new_concepts": report.new_concepts,
            "total_prereq_edges": report.total_prereq_edges,
            "dropped_edges": report.dropped_edges, "gold_protected": report.gold_protected,
            "fallback_sections": report.fallback_sections, "reused": report.reused,
        }

    @legacy_router.post("/projects/{pid}/undo-mapping")
    def undo_mapping(pid: str, body: MapBookRequest) -> dict:
        """Undo the last Book Mapping: remove only proposal-origin concepts/edges.
        Gold skeleton and all learner state are untouched."""
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        report = _LAST_MAPPING.get((pid, body.book_id))
        if report is None:
            raise HTTPException(status_code=404, detail="no mapping to undo for this book")
        svc = get_mapping_service()
        try:
            res = svc.undo_mapping(
                project_id=pid, learner_id=body.learner_id, book_id=body.book_id,
                report=report,
            )
        except ScopeError as e:
            raise HTTPException(status_code=403, detail=str(e))
        _LAST_MAPPING.pop((pid, body.book_id), None)
        return res

    @legacy_router.get("/projects/{pid}/book-graph")
    def book_graph(pid: str, book_id: str) -> dict:
        """Return the current concept graph (concepts + prerequisite edges) for
        a book, grouped by chapter for the Part/Chapter-level key view."""
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        if book_id not in repo.allowed_book_ids(pid):
            raise HTTPException(status_code=403, detail="book not in project scope")
        concepts = repo.concepts_for_book(book_id)
        chapters: dict[str, list[dict]] = {}
        for c in concepts:
            chapters.setdefault(c.chapter or "(未分类)", []).append({
                "concept_id": c.concept_id, "name": c.name, "source": c.source,
                "importance": c.importance, "difficulty": c.difficulty.value,
                "prerequisites": c.prerequisites, "related": c.related_concepts,
            })
        edges = [
            {"source": r.source_concept_id, "target": r.target_concept_id,
             "relation": r.relation.value, "source_type": r.source}
            for r in repo.relations
            if r.source_concept_id in {c.concept_id for c in concepts}
        ]
        return {"book_id": book_id, "total_concepts": len(concepts),
                "chapters": chapters, "edges": edges}

    # --- Phase 4: Learner Model & mode presets ---------------------------

    @legacy_router.post("/projects/{pid}/exposure")
    def record_exposure_endpoint(pid: str, body: ExposureRequest) -> dict:
        """Record an exposure-only event (READ/QUESTION/EXPLANATION) that moves
        the concept's exposure state but never mastery (LEARNING_MODEL §3)."""
        repo = get_repo()
        proj = repo.projects.get(pid)
        if proj is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            et = EvidenceType(body.evidence_type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown evidence_type {body.evidence_type}")
        book_id = next(iter(repo.allowed_book_ids(pid)), None)
        if book_id is None:
            raise HTTPException(status_code=400, detail="project has no linked book")
        import uuid as _uuid
        try:
            res = record_exposure(
                repo, learner_id=body.learner_id, project_id=pid, concept_id=body.concept_id,
                source_book_id=book_id, evidence_id=f"ex{_uuid.uuid4().hex[:8]}",
                evidence_type=et, occurred_at=datetime.now(timezone.utc),
                read_coverage=body.read_coverage, explicit_complete=body.explicit_complete,
                source_chunk_ids=body.source_chunk_ids,
            )
        except ScopeError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {
            "concept_id": body.concept_id,
            "exposure_state": res.exposure_state.value,
            "read_progress": res.read_progress,
            "changed": res.changed,
            "reason": res.reason,
        }

    # --- Phase 5: Misconception diagnosis closure -----------------------

    @legacy_router.post("/projects/{pid}/probe")
    def gen_probe(pid: str, body: ProbeRequest) -> dict:
        """Generate + validate a diagnostic probe for a bug. Does not write state."""
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        from ..agents.bug_library import BUG_LIBRARY
        from ..engine.task.generator import generate_probe
        from ..engine.task.validator import validate
        bug = BUG_LIBRARY.get(body.bug_id)
        if bug is None:
            raise HTTPException(status_code=404, detail=f"unknown bug {body.bug_id}")
        targets = body.target_concept_ids or list(bug.related_concepts)
        draft = generate_probe(bug, target_concept_ids=targets, level=body.level,
                               router=get_qa_service().router)
        report = validate(draft, repo, pid)
        return {
            "passed": report.passed,
            "blocked_reasons": report.blocked_reasons,
            "checks": [{"name": c.name, "passed": c.passed, "skipped": c.skipped, "detail": c.detail} for c in report.checks],
            "trusted": report.trusted.model_dump(mode="json") if report.trusted else None,
        }

    @legacy_router.post("/projects/{pid}/changed-task")
    def gen_changed_task(pid: str, body: ChangedTaskRequest) -> dict:
        """Generate + validate a changed task (stage 1/2) for a remediation."""
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        if body.stage not in (1, 2):
            raise HTTPException(status_code=400, detail="stage must be 1 or 2")
        from ..agents.bug_library import BUG_LIBRARY
        from ..engine.task.generator import generate_changed_task
        from ..engine.task.validator import validate
        bug = BUG_LIBRARY.get(body.bug_id)
        if bug is None:
            raise HTTPException(status_code=404, detail=f"unknown bug {body.bug_id}")
        targets = body.target_concept_ids or list(bug.related_concepts)
        draft = generate_changed_task(bug, stage=body.stage, target_concept_ids=targets,
                                       level=body.level, router=get_qa_service().router)
        report = validate(draft, repo, pid)
        return {
            "passed": report.passed,
            "blocked_reasons": report.blocked_reasons,
            "stage": body.stage,
            "checks": [{"name": c.name, "passed": c.passed, "skipped": c.skipped, "detail": c.detail} for c in report.checks],
            "trusted": report.trusted.model_dump(mode="json") if report.trusted else None,
        }

    @legacy_router.post("/projects/{pid}/classify-probe")
    def classify_probe(pid: str, body: ClassifyProbeRequest) -> dict:
        """Classify a probe answer against a bug's competing hypotheses."""
        if pid not in get_repo().projects:
            raise HTTPException(status_code=404, detail="project not found")
        from ..agents.bug_library import BUG_LIBRARY
        from ..engine.misconception.probe_classifier import classify_answer
        bug = BUG_LIBRARY.get(body.bug_id)
        if bug is None:
            raise HTTPException(status_code=404, detail=f"unknown bug {body.bug_id}")
        res = classify_answer(bug, body.answer_text)
        return {
            "best_hypothesis": res.best_hypothesis,
            "scores": res.scores,
            "method": res.method,
            "reason": res.reason,
        }

    @legacy_router.post("/projects/{pid}/remediation/start")
    def start_remediation_full(pid: str, body: StartRemediationRequest) -> dict:
        """Flip CONFIRMED→REMEDIATING and return the rendered remediation plan."""
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        from ..services.remediation import RemediationService
        from ..storage.in_memory import ScopeError as _ScopeError
        svc = RemediationService(repo, get_qa_service().router)
        try:
            plan = svc.start(project_id=pid, bug_id=body.bug_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except _ScopeError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return plan.model_dump(mode="json")

    @legacy_router.get("/projects/{pid}/misconceptions/{bug_id}/trace")
    def misconception_trace(pid: str, bug_id: str) -> dict:
        """Full lifecycle trace for one bug (status, evidence chain, transitions)."""
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        from ..services.misconception_view import build_trace
        trace = build_trace(repo, pid, bug_id)
        return {
            "bug_id": trace.bug_id,
            "status": trace.status,
            "evidence_score": trace.evidence_score,
            "confidence_band": trace.confidence_band,
            "hypothesis_group": trace.hypothesis_group,
            "changed_task_pass_count": trace.changed_task_pass_count,
            "changed_task_pass_fingerprints": trace.changed_task_pass_fingerprints,
            "hypothesis_cycle": trace.hypothesis_cycle,
            "remediation_version": trace.remediation_version,
            "evidence_chain": [
                {
                    "evidence_id": e.evidence_id,
                    "evidence_type": e.evidence_type,
                    "result": e.result,
                    "scoring_type": e.scoring_type,
                    "direction": e.direction,
                    "strength": e.strength,
                    "task_id": e.task_id,
                    "scenario_fingerprint": e.scenario_fingerprint,
                    "high_discrimination": e.high_discrimination,
                    "occurred_at": e.occurred_at,
                }
                for e in trace.evidence_chain
            ],
            "transitions": trace.transitions,
        }

    @legacy_router.post("/projects/{pid}/validate-task")
    def validate_task(pid: str, body: ValidateTaskRequest) -> dict:
        """Run the shared Task Validator on a draft. Gate is transparent."""
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        from ..domain.models import TaskDraft
        from ..engine.task.validator import validate
        draft = TaskDraft(
            task_id=body.task_id, task_version=body.task_version,
            target_concept_ids=body.target_concept_ids,
            evidence_for_levels=body.evidence_for_levels, rubric=body.rubric,
            prompt_text=body.prompt_text, expected_answer=body.expected_answer,
            distractors=body.distractors, is_probe=body.is_probe,
            is_changed_task=body.is_changed_task,
            discriminated_bug_ids=body.discriminated_bug_ids,
            scenario_fingerprint=body.scenario_fingerprint,
            remediation_stage=body.remediation_stage,
        )
        report = validate(draft, repo, pid)
        return {
            "passed": report.passed,
            "blocked_reasons": report.blocked_reasons,
            "checks": [{"name": c.name, "passed": c.passed, "skipped": c.skipped, "detail": c.detail} for c in report.checks],
            "trusted": report.trusted.model_dump(mode="json") if report.trusted else None,
        }

    @legacy_router.get("/projects/{pid}/learning-state")
    def learning_state(pid: str) -> list[dict]:
        """Expanded learner state for the Learning State page (ARCHITECTURE §12):
        per-level derived effective status, retrievability, review-due, the
        evidence chain, and a verified/pending/weak/due grouping."""
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        views = project_state_views(repo, pid, policy=_POLICY)
        out = []
        for v in views:
            out.append({
                "concept_id": v.concept_id,
                "concept_name": v.concept_name,
                "exposure": v.exposure,
                "read_progress": v.read_progress,
                "current_verified_level": v.current_verified_level,
                "highest_ever_level": v.highest_ever_level,
                "group": v.group,
                "levels": [
                    {
                        "level": lv.level,
                        "raw_status": lv.raw_status,
                        "effective_status": lv.effective_status,
                        "retrievability": lv.retrievability,
                        "review_due_at": lv.review_due_at,
                        "verified_at": lv.verified_at,
                        "stability_days": lv.stability_days,
                    }
                    for lv in v.levels
                ],
                "evidence": [
                    {
                        "evidence_id": e.evidence_id,
                        "evidence_type": e.evidence_type,
                        "result": e.result,
                        "required_level": e.required_level,
                        "independent": e.independent,
                        "hint_level": e.hint_level,
                        "occurred_at": e.occurred_at,
                        "task_id": e.task_id,
                    }
                    for e in v.evidence
                ],
            })
        return out

    @legacy_router.get("/health/models")
    def model_health() -> dict:
        svc = get_qa_service()
        return svc.router.healthcheck()

    # --- Phase 6: long-term recovery (LEARNING_MODEL §12) -------------------

    @legacy_router.get("/projects/{pid}/recovery")
    def recovery_plan(
        pid: str,
        last_active_at: str | None = None,
    ) -> dict:
        """Build the Recovery-page recommendation: scan high-value concepts for
        EXPIRED/UNSTABLE, rank by the §11 key, and recommend a 3-minute check
        or continuing. Read-only — no state writes (ARCHITECTURE §12)."""
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        from ..services.recovery import project_recovery_plan
        from datetime import datetime as _dt
        laa = None
        if last_active_at:
            try:
                laa = _dt.fromisoformat(last_active_at)
            except ValueError:
                raise HTTPException(status_code=400, detail="last_active_at must be ISO 8601")
        plan = project_recovery_plan(repo, pid, last_active_at=laa, policy=_POLICY)
        return plan.to_dict()

    @legacy_router.post("/projects/{pid}/recovery/choose")
    def recovery_choose(pid: str, body: RecoveryChooseRequest) -> dict:
        """Record the user's recovery choice (LEARNING_MODEL §12.5: 用户选择优先).
        Returns the resulting next Action + target concept for the decision trace."""
        repo = get_repo()
        if pid not in repo.projects:
            raise HTTPException(status_code=404, detail="project not found")
        from ..services.recovery import RecoveryService
        svc = RecoveryService(repo)
        plan = svc.build_plan(project_id=pid, policy=_POLICY)
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

    if repo is None:
        app.include_router(legacy_router)
    return app


def _build_default_app() -> FastAPI:
    """The module-level app used by the production server (uvicorn entry).

    Builds a repository from configuration (SQLite by default, PostgreSQL when
    BOOKMIND_DATABASE_URL is set). Tests never touch this — they call
    ``create_app()`` with no args, which falls back to the in-memory ``_REPO``
    so compatibility tests stay hermetic."""
    from ..storage.factory import make_repository
    try:
        repo = make_repository()
    except Exception:  # pragma: no cover - defensive: fall back to in-memory
        repo = _REPO
    return create_app(repo=repo)


app = _build_default_app()
