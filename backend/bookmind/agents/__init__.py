"""Agents — Book Mapper, Tutor, Diagnostician.

Each agent has a clear role and failure boundary (ARCHITECTURE §3). They never
write mastery, misconception, review or next-action state — the deterministic
Engine does. They produce structured proposals / judgments / answers that the
Engine and services consume.
"""

from __future__ import annotations

from .tutor import TutorAgent, TutorAnswer
from .diagnostician import DiagnosticianAgent

__all__ = ["TutorAgent", "TutorAnswer", "DiagnosticianAgent"]
