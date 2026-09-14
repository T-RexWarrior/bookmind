"""Task pipeline — ARCHITECTURE.md §3.5.

The single entry point for turning an Agent's task proposal into an immutable
:class:`TrustedTaskContext` that may produce Evidence. Both ordinary
quiz/review tasks (Tutor) and probe/changed-task (Diagnostician) pass through
the shared :mod:`validator` before reaching the learner.
"""
