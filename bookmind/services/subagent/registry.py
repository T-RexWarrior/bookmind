"""Backend registry — the single place that knows which subagents exist.

Add a new subagent by writing a :class:`SubagentBackend` and listing it here;
the capability, API and UI all discover it through these helpers. Local-CLI
backends (Claude Code, Codex, Grok CLI, Antigravity CLI, Kimi CLI, opencode,
MiMo Code, Hermes Agent, OpenClaw, DeepSeek Harness), configured remote
backends, and the in-process partner backend live in the same registry.
``local_cli`` and ``detectable`` control which backends participate in
connection discovery.
"""

from __future__ import annotations

import asyncio

from bookmind.services.subagent.antigravity import AntigravityBackend
from bookmind.services.subagent.base import SubagentBackend
from bookmind.services.subagent.claude_code import ClaudeCodeBackend
from bookmind.services.subagent.codex import CodexBackend
from bookmind.services.subagent.deepseek_harness import DeepSeekHarnessBackend
from bookmind.services.subagent.grok import GrokBackend
from bookmind.services.subagent.hermes import HermesBackend
from bookmind.services.subagent.hermes_remote import HermesRemoteBackend
from bookmind.services.subagent.kimi import KimiBackend
from bookmind.services.subagent.openclaw import OpenClawBackend
from bookmind.services.subagent.opencode_family import MimoBackend, OpencodeBackend
from bookmind.services.subagent.types import DetectResult

_BACKENDS: dict[str, SubagentBackend] = {
    backend.kind: backend
    for backend in (
        ClaudeCodeBackend(),
        CodexBackend(),
        GrokBackend(),
        AntigravityBackend(),
        KimiBackend(),
        OpencodeBackend(),
        MimoBackend(),
        HermesBackend(),
        HermesRemoteBackend(),
        OpenClawBackend(),
        DeepSeekHarnessBackend(),
    )
}


def list_backend_kinds() -> list[str]:
    """Return every connectable local, remote, and Partner backend kind."""
    return list(_BACKENDS.keys())


def get_backend(kind: str) -> SubagentBackend | None:
    return _BACKENDS.get(str(kind or "").strip())


def _detectable_backends() -> list[SubagentBackend]:
    return [
        backend
        for backend in _BACKENDS.values()
        if getattr(backend, "local_cli", True) or getattr(backend, "detectable", False)
    ]


async def detect_all() -> list[DetectResult]:
    """Probe local CLIs and configured remote backends."""
    backends = _detectable_backends()
    results = await asyncio.gather(
        *(backend.detect() for backend in backends),
        return_exceptions=True,
    )
    detections: list[DetectResult] = []
    for backend, result in zip(backends, results, strict=True):
        if isinstance(result, DetectResult):
            detections.append(result)
        else:
            detections.append(
                DetectResult(
                    kind=backend.kind,
                    display_name=backend.display_name,
                    available=False,
                    detail=str(result),
                )
            )
    return detections


__all__ = ["list_backend_kinds", "get_backend", "detect_all"]
