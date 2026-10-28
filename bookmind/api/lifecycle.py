"""Startup, recovery and shutdown for the BookMind learning runtime."""
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

logger = logging.getLogger(__name__)

CONFIG_DRIFT_ERROR_TEMPLATE = (
    "Configuration Drift Detected: Capability tool references {drift} are not "
    "registered in the runtime tool registry. Register the missing tools or "
    "remove the stale tool names from the capability manifests."
)


def validate_tool_consistency():
    """
    Validate that capability manifests only reference tools that are actually
    registered in the runtime ``ToolRegistry``.
    """
    try:
        from bookmind.runtime.registry.capability_registry import get_capability_registry
        from bookmind.runtime.registry.tool_registry import get_tool_registry

        capability_registry = get_capability_registry()
        tool_registry = get_tool_registry()
        available_tools = set(tool_registry.list_tools())

        referenced_tools = set()
        for manifest in capability_registry.get_manifests():
            referenced_tools.update(manifest.get("tools_used", []) or [])

        drift = referenced_tools - available_tools
        if drift:
            raise RuntimeError(CONFIG_DRIFT_ERROR_TEMPLATE.format(drift=drift))
    except RuntimeError:
        logger.exception("Configuration validation failed")
        raise
    except Exception:
        logger.exception("Failed to load configuration for validation")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifecycle management
    Gracefully handle startup and shutdown events, avoid CancelledError
    """
    # Execute on startup
    logger.info("Application startup")
    app.state.ready = False

    # Validate configuration consistency
    validate_tool_consistency()

    # Build one process-level dependency graph.  Every adapter resolves turns
    # through this container, so a WebSocket cannot accidentally construct a
    # second store/runtime for the same authenticated scope.
    from bookmind.app.container import get_application_container

    application_container = getattr(app.state, "application_container", None)
    if application_container is None:
        application_container = get_application_container()
        app.state.application_container = application_container
    await application_container.start()
    from bookmind.api.utils.progress_broadcaster import ProgressBroadcaster
    from bookmind.api.utils.task_log_stream import get_task_stream_manager
    from bookmind.knowledge.progress_events import install_progress_ports

    install_progress_ports(
        broadcast=ProgressBroadcaster.get_instance().broadcast,
        emit_task_event=get_task_stream_manager().emit,
    )
    migration_reports = await application_container.run_startup_data_migrations()
    legacy_reports = migration_reports["legacy_chat"]
    migrated_reports = [report for report in legacy_reports if report.get("source_hash")]
    if migrated_reports:
        logger.info(
            "Legacy chat migration complete: imported=%s skipped=%s archived=%s",
            sum(int(report.get("imported") or 0) for report in migrated_reports),
            sum(int(report.get("skipped") or 0) for report in migrated_reports),
            [report.get("archived_to", "") for report in migrated_reports],
        )
    workspace_migrated = sum(
        int(report.get("migrated") or 0) for report in migration_reports["workspace_preferences"]
    )
    if workspace_migrated:
        logger.info(
            "Workspace session migration complete: migrated=%s scopes=%s",
            workspace_migrated,
            len(migration_reports["workspace_preferences"]),
        )

    try:
        from bookmind.learning.assessment import reconcile_linked_assessments

        recovered, failed = await reconcile_linked_assessments()
        if recovered or failed:
            logger.info("Linked assessment recovery: recovered=%s failed=%s", recovered, failed)
    except Exception:
        logger.exception("Failed to reconcile linked assessments at startup")

    # Initialize LLM client early so OPENAI_* env vars are available before
    # any downstream provider integrations start.
    try:
        from bookmind.services.llm import get_llm_client

        llm_client = get_llm_client()
        logger.info(f"LLM client initialized: model={llm_client.config.model}")
    except Exception as e:
        logger.warning(f"Failed to initialize LLM client at startup: {e}")

    try:
        from bookmind.events.event_bus import get_event_bus

        event_bus = get_event_bus()
        await event_bus.start()
        logger.info("EventBus started")
    except Exception as e:
        logger.warning(f"Failed to start EventBus: {e}")

    from bookmind.runtime.background_leader import BackgroundLeaderSupervisor

    background_supervisor = BackgroundLeaderSupervisor(
        application_container.coordinator,
        application_container.worker_id,
        recovery_callback=application_container.recover_once,
        renew_interval_seconds=application_container.settings.renew_interval_seconds,
        recovery_interval_seconds=application_container.settings.recovery_interval_seconds,
    )
    app.state.background_supervisor = background_supervisor
    await background_supervisor.start()

    # Ping PocketBase if configured — logs a warning (not an error) if unreachable
    try:
        from bookmind.services.pocketbase_client import ping_pocketbase

        await ping_pocketbase()
    except Exception as e:
        logger.warning(f"PocketBase startup check failed: {e}")

    app.state.ready = True
    yield

    # Execute on shutdown
    app.state.ready = False
    logger.info("Application shutdown")

    install_progress_ports(broadcast=None, emit_task_event=None)

    try:
        await background_supervisor.close()
        logger.info("Leader-owned background services stopped")
    except Exception as e:
        logger.warning(f"Failed to stop background leader: {e}")

    try:
        await application_container.close()
        logger.info("Application container closed")
    except Exception as e:
        logger.warning(f"Failed to close application container: {e}")

    # Close MCP server connections. Each one owns an AsyncExitStack inside its
    # own task, so they must be torn down here rather than left to interpreter
    # exit (stdio servers would otherwise leak child processes).
    try:
        from bookmind.services.mcp import get_mcp_manager

        await get_mcp_manager().shutdown()
        logger.info("MCP connections closed")
    except Exception as e:
        logger.warning(f"Failed to close MCP connections: {e}")

    # Close pooled LLM SDK clients so their keep-alive sockets and transports
    # are released deterministically instead of waiting for interpreter GC.
    try:
        from bookmind.services.llm.provider_factory import close_runtime_provider_pool

        await close_runtime_provider_pool()
        logger.info("LLM provider pool closed")
    except Exception as e:
        logger.warning(f"Failed to close LLM provider pool: {e}")

    try:
        from bookmind.runtime.agentic.client import close_agentic_client_pool

        await close_agentic_client_pool()
        logger.info("Agentic LLM client pool closed")
    except Exception as e:
        logger.warning(f"Failed to close agentic LLM client pool: {e}")

    # Stop EventBus
    try:
        from bookmind.events.event_bus import get_event_bus

        event_bus = get_event_bus()
        await event_bus.stop()
        logger.info("EventBus stopped")
    except Exception as e:
        logger.warning(f"Failed to stop EventBus: {e}")
