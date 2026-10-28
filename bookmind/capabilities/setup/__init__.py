"""Setup loop capability — BookMind inspecting and changing its own configuration."""

from bookmind.capabilities.setup.capability import SetupCapability
from bookmind.capabilities.setup.tools import SETUP_TOOL_NAMES, SETUP_TOOL_TYPES

__all__ = ["SETUP_TOOL_NAMES", "SETUP_TOOL_TYPES", "SetupCapability"]
