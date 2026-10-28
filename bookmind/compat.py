"""Read legacy deployment identifiers at the boundary; write BookMind names.

Persisted content and database IDs stay intact. Explicit new settings win.
"""
from __future__ import annotations

from collections.abc import MutableMapping
import os

LEGACY_ENV_PREFIX = "DEEPTUTOR_"


def migrate_environment(environ: MutableMapping[str, str] | None = None) -> None:
    environ = os.environ if environ is None else environ
    for key, value in list(environ.items()):
        if key.startswith(LEGACY_ENV_PREFIX):
            environ.setdefault("BOOKMIND_" + key[len(LEGACY_ENV_PREFIX):], value)
