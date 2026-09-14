"""Storage boundary errors independent of any repository implementation."""


class ScopeError(Exception):
    """Raised on a cross-project, cross-user, or unknown-resource access."""
