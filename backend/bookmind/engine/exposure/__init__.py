"""Exposure state machine — LEARNING_MODEL.md §3.

Exposure describes only the *fact of textbook contact*, never a cognitive
verdict. It is the one piece of learner state that READ/QUESTION/EXPLANATION
evidence is allowed to move:

    NONE → SEEN      content first enters the effective visible area
    SEEN → COMPLETED user explicitly marks done, or read coverage crosses the
                     versioned ``read_coverage_threshold``

Crucially (§3): "曝光事件无论持续多久都不能通过 Evidence Gate" — exposure
never upgrades mastery. The Gate independently rejects exposure-only evidence
types, so this module's outputs can never leak into mastery verification.

Exposure is derived deterministically from exposure evidence + read progress,
never written by an Agent.
"""
