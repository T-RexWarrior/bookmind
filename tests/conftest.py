"""Pytest configuration: make the backend package importable."""

from __future__ import annotations

import os
import sys

# Isolate tests from a developer's local .env. config._load_dotenv runs at import
# time and would otherwise inject a real USTC_LLM_API_KEY into os.environ, turning
# "offline" routers live and making tests hit the network (and depend on a model's
# nondeterminism). Set the opt-out before bookmind.config is ever imported.
os.environ.setdefault("BOOKMIND_NO_DOTENV", "1")
# Also belt-and-braces: if a key was already in the real environment, tests that
# build a router from get_settings() would still go live. The test suite is a
# deterministic, offline contract; clear it for the duration of the run.
os.environ.pop("USTC_LLM_API_KEY", None)

# Add backend/ to sys.path so ``import bookmind`` works from anywhere.
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "..", "backend")
sys.path.insert(0, BACKEND)
