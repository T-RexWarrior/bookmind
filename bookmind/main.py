"""Canonical ASGI entry point: python -m uvicorn bookmind.main:app."""
from bookmind.api.main import app

__all__ = ["app"]
