"""Misconception evidence scoring — LEARNING_MODEL.md §8.

A fixed, explainable scoring scheme — *not* a probability::

    WEAK support            +1
    MEDIUM support          +2
    STRONG support          +3
    high-discrimination
      probe support         +3
    explicit disproof       -2
    changed-task indep PASS -2

Each Evidence contributes *exactly one* scoring type. Replaying the ledger
must not double-count (keyed by evidence_id).

Confidence bands (display only, never a probability):
    score < 4  → LOW
    4–5        → MEDIUM
    >= 6       → HIGH
"""

from __future__ import annotations

from ...domain.enums import ConfidenceBand, EvidenceType
from ...domain.models import Evidence


def scoring_type(evidence: Evidence) -> str:
    """Classify an evidence into exactly one fixed-score bucket.

    Precedence (mutually exclusive — first match wins):
      1. changed-task independent PASS  → ``changed_task_pass``  (-2)
      2. explicit disproof (AGAINST)    → ``explicit_disproof``  (-2)
      3. high-discrimination probe FOR  → ``probe_high_disc``    (+3)
      4. probe FOR                      → ``probe``              (strength-weighted)
      5. plain FOR signal               → ``support``            (strength-weighted)
      6. otherwise                      → ``none``               (0)
    """
    # 1. changed-task independent PASS is itself a disproof of the bug.
    if (
        evidence.evidence_type == EvidenceType.CHANGED_TASK
        and evidence.result == "PASS"
        and evidence.independent
    ):
        return "changed_task_pass"
    # 2. explicit AGAINST signal.
    if evidence.misconception_signals:
        against = [s for s in evidence.misconception_signals if s.direction.value == "AGAINST"]
        if against:
            return "explicit_disproof"
    # 3. high-discrimination probe FOR.
    if evidence.high_discrimination and evidence.evidence_type == EvidenceType.PROBE:
        return "probe_high_disc"
    # 4/5. probe or plain support, weighted by strength.
    if evidence.misconception_signals:
        for_sig = [s for s in evidence.misconception_signals if s.direction.value == "FOR"]
        if for_sig:
            if evidence.evidence_type == EvidenceType.PROBE:
                return "probe"
            return "support"
    return "none"


def score_for(evidence: Evidence) -> int:
    """The integer score delta this evidence contributes."""
    kind = scoring_type(evidence)
    if kind == "changed_task_pass":
        return -2
    if kind == "explicit_disproof":
        return -2
    if kind == "probe_high_disc":
        return +3
    if kind in ("probe", "support"):
        # weighted by strength of the FOR signal
        for s in evidence.misconception_signals:
            if s.direction.value == "FOR":
                if s.strength.value == "WEAK":
                    return +1
                if s.strength.value == "MEDIUM":
                    return +2
                return +3
        return 0
    return 0


def confidence_band(score: int) -> ConfidenceBand:
    if score < 4:
        return ConfidenceBand.LOW
    if score <= 5:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.HIGH


def is_high_discrimination_support(evidence: Evidence) -> bool:
    return scoring_type(evidence) == "probe_high_disc"
