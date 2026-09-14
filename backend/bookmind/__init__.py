"""BookMind / 学迹 — cognitive state-tracking & active-learning agent for textbooks.

Package layout follows the architecture's dependency rule:

    API → Services → Agents / Engine / Retrieval → Repository / VectorStore

The Learning Engine (``bookmind.engine``) is deterministic and is the *only*
place that writes mastery, misconception, review and next-action state. Agents
produce structured proposals and natural language; they never write state.
"""

__version__ = "0.1.0"
