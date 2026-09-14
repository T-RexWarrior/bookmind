"""Misconception state machine — LEARNING_MODEL.md §8.

Deterministic transitions driven by the fixed evidence score and task outcomes.
The Engine, not any Agent, decides status changes. This module is pure: given a
hypothesis + the list of its contributing evidence, it returns the updated
hypothesis and a description of the transition.

State-entry thresholds (from LEARNING_MODEL.md §8):

    SUSPECTED:  score >= 2
    LIKELY:     score >= 4
    CONFIRMED:  score >= 6  AND  >= 2 distinct tasks  AND  >= 1 probe evidence
    DISMISSED:  not yet CONFIRMED  AND  score <= 0  AND  >= 1 explicit disproof

Transition table (LEARNING_MODEL.md §8 failure/recovery paths):

    SUSPECTED/LIKELY   explicit disproof & DISMISSED-cond   → DISMISSED
    DISMISSED          new support → score >= 2             → SUSPECTED (new cycle)
    CONFIRMED          start remediation                     → REMEDIATING
    REMEDIATING        first changed-task indep PASS         → VERIFYING (pass=1)
    REMEDIATING/VERIFY changed-task PARTIAL                 → stay
    REMEDIATING/VERIFY changed-task FAIL                    → CONFIRMED (reset pass)
    REMEDIATING/VERIFY new high-disc support                → CONFIRMED
    VERIFYING          2nd distinct-scenario indep PASS     → RESOLVED (pass=2)
    RESOLVED           new high-disc support                → RELAPSED
    RELAPSED           probe reconfirms                     → CONFIRMED
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.enums import (
    ConfidenceBand,
    EvidenceResult,
    EvidenceType,
    MisconceptionStatus,
)
from ...domain.models import Evidence, MisconceptionHypothesis
from .scoring import confidence_band, is_high_discrimination_support, score_for


@dataclass
class TransitionResult:
    hypothesis: MisconceptionHypothesis
    old_status: MisconceptionStatus
    new_status: MisconceptionStatus
    reason: str
    changed: bool


def _distinct_task_count(evidence: list[Evidence]) -> int:
    return len({e.task_id for e in evidence if e.task_id})


def _has_probe(evidence: list[Evidence]) -> bool:
    return any(e.evidence_type == EvidenceType.PROBE for e in evidence)


def _meets_confirmed_criteria(score: int, evidence: list[Evidence]) -> bool:
    return score >= 6 and _distinct_task_count(evidence) >= 2 and _has_probe(evidence)


def _meets_dismissed_criteria(score: int, evidence: list[Evidence]) -> bool:
    has_disproof = any(
        e.misconception_signals and any(s.direction.value == "AGAINST" for s in e.misconception_signals)
        for e in evidence
    )
    # changed-task PASS also counts as disproof.
    has_disproof = has_disproof or any(
        e.evidence_type == EvidenceType.CHANGED_TASK
        and e.result == EvidenceResult.PASS
        and e.independent
        for e in evidence
    )
    return score <= 0 and has_disproof


def _entry_status(score: int, evidence: list[Evidence]) -> MisconceptionStatus:
    """The status implied purely by score + evidence shape (no history)."""
    if _meets_confirmed_criteria(score, evidence):
        return MisconceptionStatus.CONFIRMED
    if score >= 4:
        return MisconceptionStatus.LIKELY
    if score >= 2:
        return MisconceptionStatus.SUSPECTED
    return MisconceptionStatus.SUSPECTED  # floor; DISMISSED handled separately


def update(
    hypothesis: MisconceptionHypothesis,
    evidence: list[Evidence],
    *,
    new_evidence: list[Evidence] | None = None,
    remediation_started: bool = False,
) -> TransitionResult:
    """Recompute status from the full evidence list + lifecycle signals.

    ``evidence`` is the *complete* ledger for this hypothesis (used for the
    fixed score, CONFIRMED/DISMISSED gating — deterministic replay).
    ``new_evidence`` is just the evidence submitted in *this* turn (used for
    "new high-discrimination support" / RELAPSED triggers, which must not fire
    on historical probes). If ``new_evidence`` is omitted it defaults to
    ``evidence`` (whole-list replay, convenient for from-scratch tests).

    ``remediation_started`` is set by the decision layer when REMEDIATE is
    chosen for a CONFIRMED bug; it flips CONFIRMED → REMEDIATING.
    """
    if new_evidence is None:
        new_evidence = list(evidence)

    old_status = hypothesis.status
    score = sum(score_for(e) for e in evidence)
    band = confidence_band(score)

    new = hypothesis.model_copy()
    new.evidence_score = score
    new.confidence_band = band

    status = old_status
    reason = ""

    # The latest changed-task in the *new* submission drives the remediation
    # cycle; historical changed tasks already counted in pass_fingerprints.
    new_changed_tasks = [e for e in new_evidence if e.evidence_type == EvidenceType.CHANGED_TASK]
    latest_ct = new_changed_tasks[-1] if new_changed_tasks else None
    new_high_disc = any(is_high_discrimination_support(e) for e in new_evidence)
    new_probe = [e for e in new_evidence if e.evidence_type == EvidenceType.PROBE]

    # --- Confirmed gating -------------------------------------------------
    confirmed_ok = _meets_confirmed_criteria(score, evidence)

    if status in (MisconceptionStatus.SUSPECTED, MisconceptionStatus.LIKELY):
        if _meets_dismissed_criteria(score, evidence):
            status = MisconceptionStatus.DISMISSED
            reason = "explicit disproof and score<=0 before confirmation"
        elif confirmed_ok:
            status = MisconceptionStatus.CONFIRMED
            reason = "score>=6, >=2 tasks, >=1 probe"
        else:
            status = _entry_status(score, evidence)
            reason = f"score={score} → {_entry_status(score, evidence).value}"

    elif status == MisconceptionStatus.DISMISSED:
        new_support = any(
            s for e in new_evidence for s in e.misconception_signals if s.direction.value == "FOR"
        )
        if score >= 2 and new_support:
            status = MisconceptionStatus.SUSPECTED
            new.hypothesis_cycle = hypothesis.hypothesis_cycle + 1
            reason = "new support after dismissal; new hypothesis cycle"

    elif status == MisconceptionStatus.CONFIRMED:
        if remediation_started:
            status = MisconceptionStatus.REMEDIATING
            new.remediation_version = (hypothesis.remediation_version or 0) + 1
            new.changed_task_pass_count = 0
            new.changed_task_pass_fingerprints = []
            reason = "remediation started"
        elif not confirmed_ok and _meets_dismissed_criteria(score, evidence):
            status = MisconceptionStatus.DISMISSED
            reason = "fell below confirmation with disproof"
        # otherwise stay CONFIRMED.

    elif status == MisconceptionStatus.REMEDIATING:
        if latest_ct is not None:
            if latest_ct.result == EvidenceResult.PASS and latest_ct.independent:
                fp = latest_ct.scenario_fingerprint
                passes = [f for f in hypothesis.changed_task_pass_fingerprints if f != fp]
                passes.append(fp)
                new.changed_task_pass_fingerprints = passes
                if len(passes) == 1:
                    status = MisconceptionStatus.VERIFYING
                    new.changed_task_pass_count = 1
                    reason = "first changed-task PASS (distinct scenario)"
                elif len(passes) >= 2:
                    status = MisconceptionStatus.RESOLVED
                    new.changed_task_pass_count = 2
                    reason = "two distinct-scenario changed-task PASS"
            elif latest_ct.result == EvidenceResult.PARTIAL:
                reason = "changed-task PARTIAL; stay REMEDIATING"
            elif latest_ct.result == EvidenceResult.FAIL:
                status = MisconceptionStatus.CONFIRMED
                new.changed_task_pass_count = 0
                new.changed_task_pass_fingerprints = []
                reason = "changed-task FAIL → back to CONFIRMED"
        # Only a *new* high-discrimination probe re-opens remediation.
        if new_high_disc and status == MisconceptionStatus.REMEDIATING:
            status = MisconceptionStatus.CONFIRMED
            reason = "new high-discrimination support during remediation"

    elif status == MisconceptionStatus.VERIFYING:
        if latest_ct is not None:
            if latest_ct.result == EvidenceResult.PASS and latest_ct.independent:
                fp = latest_ct.scenario_fingerprint
                passes = [f for f in hypothesis.changed_task_pass_fingerprints if f != fp]
                passes.append(fp)
                new.changed_task_pass_fingerprints = passes
                if len(passes) >= 2:
                    status = MisconceptionStatus.RESOLVED
                    new.changed_task_pass_count = 2
                    reason = "second distinct-scenario changed-task PASS → RESOLVED"
                else:
                    new.changed_task_pass_count = 1
                    reason = "duplicate-scenario PASS does not advance"
            elif latest_ct.result == EvidenceResult.PARTIAL:
                reason = "changed-task PARTIAL; stay VERIFYING"
            elif latest_ct.result == EvidenceResult.FAIL:
                status = MisconceptionStatus.CONFIRMED
                new.changed_task_pass_count = 0
                new.changed_task_pass_fingerprints = []
                reason = "changed-task FAIL during verification → CONFIRMED"
        if new_high_disc:
            status = MisconceptionStatus.CONFIRMED
            reason = "new high-discrimination support during verification"

    elif status == MisconceptionStatus.RESOLVED:
        if new_high_disc:
            status = MisconceptionStatus.RELAPSED
            reason = "new high-discrimination support after RESOLVED → RELAPSED"

    elif status == MisconceptionStatus.RELAPSED:
        if any(is_high_discrimination_support(e) for e in new_probe):
            status = MisconceptionStatus.CONFIRMED
            new.remediation_version = (hypothesis.remediation_version or 0) + 1
            new.changed_task_pass_count = 0
            new.changed_task_pass_fingerprints = []
            reason = "probe reconfirmed → new remediation cycle"

    new.status = status
    new.evidence_ids = [e.evidence_id for e in evidence]
    changed = status != old_status

    if not reason:
        reason = f"score={score}; status unchanged at {status.value}"

    return TransitionResult(
        hypothesis=new,
        old_status=old_status,
        new_status=status,
        reason=reason,
        changed=changed,
    )
