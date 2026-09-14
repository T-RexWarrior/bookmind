"""Application-layer errors shared by services and transport adapters.

The core application must not depend on FastAPI.  HTTP-specific rendering
lives in :mod:`bookmind.api.errors`; services raise this transport-neutral
exception and other entry points may translate it differently.
"""

from __future__ import annotations


class AppError(Exception):
    """A stable, user-safe application error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        can_retry: bool = False,
        action: str = "",
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.can_retry = can_retry
        self.action = action
        super().__init__(message)
