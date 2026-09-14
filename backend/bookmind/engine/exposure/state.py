"""Exposure transitions — LEARNING_MODEL.md §3.

Pure: given a state + one exposure-affecting event, returns the updated
``exposure_state`` and ``read_progress``. No I/O.

The two legal transitions:

    NONE  → SEEN        on first visible contact (READ/QUESTION/EXPLANATION)
    SEEN  → COMPLETED   on explicit completion OR read coverage ≥ threshold

``COMPLETED`` is sticky — once completed, later exposure events never revert
it. ``read_progress`` is clamped to [0, 1] and only ever increases (reading
is monotonic within a section).
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.enums import ExposureState
from ...domain.models import LearnerConceptState


@dataclass(frozen=True)
class ExposureEvent:
    """One exposure-affecting event applied to a concept's state."""

    kind: str  # "seen" | "read_progress" | "completed"
    # For "read_progress": the new cumulative coverage fraction in [0, 1].
    coverage: float | None = None
    # For "completed": whether the user explicitly marked it done.
    explicit: bool = False


@dataclass
class ExposureResult:
    exposure_state: ExposureState
    read_progress: float
    changed: bool
    reason: str


def apply_exposure(
    state: LearnerConceptState,
    event: ExposureEvent,
    *,
    read_coverage_threshold: float = 0.9,
) -> ExposureResult:
    """Apply one exposure event, returning the new exposure_state + progress.

    ``read_coverage_threshold`` is the versioned policy value at which SEEN
    promotes to COMPLETED automatically (LEARNING_MODEL §3: "达到版本化阅读
    覆盖阈值时变为 COMPLETED").
    """
    old_exp = state.exposure_state
    old_prog = state.read_progress
    new_exp = old_exp
    new_prog = old_prog
    reason = ""

    if event.kind == "seen":
        if old_exp == ExposureState.NONE:
            new_exp = ExposureState.SEEN
            reason = "first visible contact → SEEN"
        else:
            reason = f"already {old_exp.value}; no transition"

    elif event.kind == "read_progress":
        cov = max(0.0, min(1.0, event.coverage or 0.0))
        # Reading progress is monotonic — never decreases within a concept.
        new_prog = max(old_prog, cov)
        if old_exp == ExposureState.NONE:
            new_exp = ExposureState.SEEN
            reason = "read progress implies contact → SEEN"
        if (
            old_exp != ExposureState.COMPLETED
            and new_prog >= read_coverage_threshold
        ):
            new_exp = ExposureState.COMPLETED
            reason = f"coverage {new_prog:.2f} ≥ threshold → COMPLETED"
        elif not reason:
            reason = f"progress → {new_prog:.2f}"

    elif event.kind == "completed":
        if old_exp != ExposureState.COMPLETED:
            new_exp = ExposureState.COMPLETED
            new_prog = 1.0 if event.explicit else new_prog
            reason = "explicit completion → COMPLETED"
        else:
            reason = "already COMPLETED"

    changed = new_exp != old_exp or new_prog != old_prog
    return ExposureResult(
        exposure_state=new_exp,
        read_progress=round(new_prog, 6),
        changed=changed,
        reason=reason or "no change",
    )


def is_exposure_only(evidence_type_value: str) -> bool:
    """True for the three evidence types that may only move exposure, never
    mastery (LEARNING_MODEL §4)."""
    return evidence_type_value in ("READ", "QUESTION", "EXPLANATION")
