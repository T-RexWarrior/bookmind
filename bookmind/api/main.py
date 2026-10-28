import logging
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bookmind.__version__ import __version__
from bookmind.api.security_guard import install_security_guard
from bookmind.logging import configure_logging
from bookmind.services.config import (
    ensure_runtime_settings_files,
    export_runtime_settings_to_env,
    load_auth_settings,
    load_system_settings,
)
from bookmind.services.config.origins import normalize_origins
from bookmind.services.path_service import get_path_service

ensure_runtime_settings_files()
export_runtime_settings_to_env(overwrite=True)
configure_logging()
logger = logging.getLogger(__name__)


class _SuppressWsNoise(logging.Filter):
    """Suppress noisy uvicorn logs for WebSocket connection churn."""

    _SUPPRESSED = ("connection open", "connection closed")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(f in msg for f in self._SUPPRESSED)


logging.getLogger("uvicorn.error").addFilter(_SuppressWsNoise())

def _build_cors_settings() -> dict[str, object]:
    """Build CORS settings for both localhost and remote Docker deployments."""
    system_settings = load_system_settings()
    auth_settings = load_auth_settings()
    frontend_port = str(system_settings["frontend_port"])
    extra_origins = normalize_origins(
        [system_settings["cors_origin"], system_settings["cors_origins"]]
    )
    origins = [
        f"http://localhost:{frontend_port}",
        f"http://127.0.0.1:{frontend_port}",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    for origin in extra_origins:
        if origin not in origins:
            origins.append(origin)

    # Auth is disabled by default. In that local/single-user mode, mirror the
    # pre-v1.3.8 behavior and allow remote Docker/LAN origins out of the box.
    # When auth is enabled, require explicit CORS_ORIGIN(S) for credentialed
    # cross-origin requests.
    allow_origin_regex = None if auth_settings["enabled"] else r"https?://.*"
    mode = "explicit" if auth_settings["enabled"] else "permissive"
    return {
        "allow_origins": origins,
        "allow_origin_regex": allow_origin_regex,
        "mode": mode,
    }


from bookmind.api.lifecycle import lifespan


from bookmind.services.workspace.activity import WorkspaceActivityMiddleware

app = FastAPI(
    title="BookMind API",
    version=__version__,
    lifespan=lifespan,
    # Disable automatic trailing slash redirects to prevent protocol downgrade issues
    # when deployed behind HTTPS reverse proxies (e.g., nginx).
    # Without this, FastAPI's 307 redirects may change HTTPS to HTTP.
    # See: https://github.com/HKUDS/DeepTutor/issues/112
    redirect_slashes=False,
)
app.add_middleware(WorkspaceActivityMiddleware)


@app.middleware("http")
async def json_error_boundary(request: Request, call_next):
    """Catch-all so 500s always return JSON, never Starlette's plain-text body.

    Registered as a middleware rather than an ``@app.exception_handler``: a
    handler for ``Exception`` is installed on Starlette's outermost
    ``ServerErrorMiddleware``, so its response skips every middleware added
    here — the 500 would carry no CORS headers (a cross-origin caller sees an
    opaque CORS failure instead of this body) and would never reach the access
    log below. Registered *before* ``CORSMiddleware``, this boundary sits
    inside it, so the response travels back out through the normal stack.
    """
    try:
        return await call_next(request)
    except Exception as exc:
        logger.error(
            "Unhandled exception on %s %s: %s",
            request.method,
            request.url.path,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"{type(exc).__name__}: {exc}",
                "type": type(exc).__name__,
            },
        )


# Optional fastapi-guard screening (off unless security_guard.enabled). Added
# before the access log and CORS middlewares below: Starlette makes the last-added
# middleware outermost, so CORS stays outside the guard (429/403 carry CORS
# headers) and the access log sees what the guard rejects.
install_security_guard(app)

# Access logging is funneled through this one middleware. uvicorn's own
# per-request access log is disabled on every launch path (run_server.py via
# access_log=False; the launcher and Docker via `--no-access-log`), so routine
# 200s — the chatty frontend polling of /settings, /tools, /knowledge-bases,
# etc. — never reach the logs. Only non-200s are surfaced, since those are the
# ones worth seeing.
#
# The `bookmind.access` logger gets its own INFO stdout handler rather than
# leaning on the root handlers: the root console handler runs at the global log
# level (WARNING by default), which would swallow these INFO access lines.
# propagate=False keeps them from also printing through root if the global
# level is ever lowered to INFO/DEBUG.
_access_logger = logging.getLogger("bookmind.access")
if not any(getattr(h, "_bookmind_access_handler", False) for h in _access_logger.handlers):
    _access_handler = logging.StreamHandler(sys.stdout)
    _access_handler.setLevel(logging.INFO)
    _access_handler.setFormatter(logging.Formatter("%(message)s"))
    _access_handler._bookmind_access_handler = True  # type: ignore[attr-defined]
    _access_logger.addHandler(_access_handler)
    _access_logger.setLevel(logging.INFO)
    _access_logger.propagate = False


@app.middleware("http")
async def selective_access_log(request, call_next):
    response = await call_next(request)
    if response.status_code != 200:
        _access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            request.client.host if request.client else "-",
            request.method,
            request.url.path,
            request.scope.get("http_version", "1.1"),
            response.status_code,
        )
    return response


_cors_settings = _build_cors_settings()
logger.info(
    "CORS configured: mode=%s allow_origins=%s allow_origin_regex=%s",
    _cors_settings["mode"],
    _cors_settings["allow_origins"],
    _cors_settings["allow_origin_regex"],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_settings["allow_origins"],
    allow_origin_regex=_cors_settings["allow_origin_regex"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize user directories on startup
try:
    from bookmind.services.setup import init_user_directories

    init_user_directories()
except Exception:
    # Fallback: just create the main directory if it doesn't exist
    user_dir = get_path_service().get_public_outputs_root()
    if not user_dir.exists():
        user_dir.mkdir(parents=True)

from bookmind.api.routes import register_routes

register_routes(app)


@app.get("/")
async def root():
    return {"message": "Welcome to BookMind API"}


@app.get("/health/live")
async def health_live():
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready(request: Request):
    if not bool(getattr(request.app.state, "ready", False)):
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    container = getattr(request.app.state, "application_container", None)
    if container is None or not await container.coordinator.health():
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "coordination_unavailable"},
        )
    return {"status": "ready"}


if __name__ == "__main__":
    from bookmind.api.run_server import main as run_server_main

    run_server_main()
