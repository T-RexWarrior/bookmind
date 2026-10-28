"""Register the supported BookMind HTTP and WebSocket surfaces.

Called after runtime settings initialization; optional integration routers stay
unimported unless the application actually mounts them.
"""
from fastapi import Depends, FastAPI


def register_routes(app: FastAPI) -> None:
    from bookmind.api.routers import (
        attachments,
        auth,
        book,
        capabilities,
        capabilities_settings,
        file_preview,
        knowledge,
        mastery_path,
        notebook,
        outputs,
        practice,
        question_notebook,
        quiz_judge,
        reading,
        reading_extensions,
        sessions,
        settings,
        system,
        unified_ws,
        visualizers,
        workspace,
    )
    from bookmind.api.routers import tools as tools_router
    from bookmind.api.routers.file_library import router as file_library_router
    from bookmind.api.routers.auth import require_auth, require_learning_surface
    from bookmind.textbook.router import router as textbook_router

    # Auth router is public — login/logout/register/status require no token
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(outputs.router, prefix="/files/outputs", tags=["outputs"])
    app.include_router(
        workspace.files_router,
        prefix="/files/workspace-items",
        tags=["workspace"],
    )

    # All other routers require a valid session when AUTH_ENABLED=true.
    # require_auth is a no-op when AUTH_ENABLED=false, so this is safe for local use.

    _auth = [Depends(require_learning_surface)]

    app.include_router(knowledge.router, prefix="/api", tags=["knowledge-bases"], dependencies=_auth)
    app.include_router(
        file_preview.router,
        prefix="/api/file-preview",
        tags=["file-preview"],
        dependencies=[Depends(require_auth)],
    )
    app.include_router(
        mastery_path.router,
        prefix="/api/mastery-paths",
        tags=["mastery-path"],
        dependencies=_auth,
    )
    app.include_router(
        file_library_router,
        prefix="/files/library",
        tags=["library"],
        dependencies=_auth,
    )
    # WebSocket handlers authenticate inside the connection before ``accept``.
    # Keep them off HTTP router dependencies: ``require_learning_surface`` takes a
    # Request, which FastAPI cannot construct for a WebSocket scope.
    app.include_router(knowledge.ws_router, prefix="/ws", tags=["knowledge-bases"])
    app.include_router(
        mastery_path.ws_router,
        prefix="/ws",
        tags=["mastery-path"],
    )
    app.include_router(notebook.router, prefix="/api", tags=["notebooks"], dependencies=_auth)
    app.include_router(book.router, prefix="/api", tags=["books"], dependencies=_auth)
    app.include_router(book.ws_router, prefix="/ws", tags=["books"])
    app.include_router(reading.router, prefix="/api/reading", tags=["reading"], dependencies=_auth)
    app.include_router(
        reading_extensions.router,
        prefix="/api/reading",
        tags=["reading-extensions"],
        dependencies=_auth,
    )
    app.include_router(
        capabilities_settings.router,
        prefix="/api/capabilities",
        tags=["capabilities"],
        dependencies=_auth,
    )
    app.include_router(
        capabilities.router,
        prefix="/api/capabilities",
        tags=["capabilities"],
        dependencies=_auth,
    )
    app.include_router(sessions.router, prefix="/api/sessions", tags=["sessions"], dependencies=_auth)
    app.include_router(
        question_notebook.router,
        prefix="/api/question-notebook",
        tags=["question-notebook"],
        dependencies=_auth,
    )
    app.include_router(
        practice.router,
        prefix="/api/question-notebook/practice",
        tags=["practice"],
        dependencies=_auth,
    )
    # Public UI-settings read (auth pages bootstrap the interface language
    # before a session exists, so GET /api/settings/ui must not be gated
    # by _auth). Mounted first so the path resolves here, not on the gated
    # settings router below.
    app.include_router(
        settings.public_router,
        prefix="/api/settings",
        tags=["settings"],
    )
    app.include_router(settings.router, prefix="/api/settings", tags=["settings"], dependencies=_auth)
    app.include_router(
        workspace.settings_router,
        prefix="/api/settings/workspace",
        tags=["workspace-settings"],
        dependencies=_auth,
    )
    app.include_router(tools_router.router, prefix="/api/tools", tags=["tools"], dependencies=_auth)
    app.include_router(system.router, prefix="/api/system", tags=["system"], dependencies=_auth)
    app.include_router(
        visualizers.router,
        prefix="/api/visualizers",
        tags=["visualizers"],
        dependencies=_auth,
    )
    app.include_router(
        attachments.router,
        prefix="/files/attachments",
        tags=["attachments"],
        dependencies=_auth,
    )

    # Unified WebSocket endpoint — auth is checked inside the handler (WebSockets
    # cannot use FastAPI dependencies in the standard way)
    app.include_router(unified_ws.router, tags=["unified-ws"])

    # Quiz AI-judge WebSocket — same caveat as unified_ws above; auth is checked
    # inside the handler so the WS upgrade isn't rejected by an HTTP-style dep.
    app.include_router(quiz_judge.router, prefix="/ws", tags=["quiz-judge"])


    app.include_router(textbook_router, dependencies=_auth)
