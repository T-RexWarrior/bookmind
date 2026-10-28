"""Chat channels module with plugin architecture."""

from bookmind.partners.channels.base import BaseChannel
from bookmind.partners.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
