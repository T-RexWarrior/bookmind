"""First-class Partner Group services."""

from bookmind.services.partner_groups.manager import (
    PartnerGroupManager,
    get_partner_group_manager,
)
from bookmind.services.partner_groups.memory import shared_memory_registry
from bookmind.services.partner_groups.models import PartnerGroupConfig, PartnerInvocation
from bookmind.services.partner_groups.modes import discussion_mode_registry

__all__ = [
    "PartnerGroupConfig",
    "PartnerGroupManager",
    "PartnerInvocation",
    "discussion_mode_registry",
    "get_partner_group_manager",
    "shared_memory_registry",
]
