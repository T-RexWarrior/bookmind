"""MarginNote 4 loop capability -- agentic retrieval over a synced MN4 library."""

from bookmind.capabilities.marginnote4.capability import MarginNoteCapability
from bookmind.capabilities.marginnote4.tools import (
    MARGINNOTE_TOOL_NAMES,
    MARGINNOTE_TOOL_TYPES,
)

__all__ = [
    "MARGINNOTE_TOOL_NAMES",
    "MARGINNOTE_TOOL_TYPES",
    "MarginNoteCapability",
]
