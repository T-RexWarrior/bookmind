"""Obsidian loop capability — agentic retrieval & authoring over a connected vault."""

from bookmind.capabilities.obsidian.capability import ObsidianCapability
from bookmind.capabilities.obsidian.tools import OBSIDIAN_TOOL_NAMES, OBSIDIAN_TOOL_TYPES

__all__ = ["OBSIDIAN_TOOL_NAMES", "OBSIDIAN_TOOL_TYPES", "ObsidianCapability"]
