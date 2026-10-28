"""BookMind learning application and reusable learning engines."""
import os

from .compat import migrate_environment

migrate_environment()
os.environ.setdefault("BOOKMIND_APP", "1")
