"""Misconception trace view — a read-side projection (LEARNING_MODEL §13).

Analogous to :mod:`~bookmind.services.learner_state_view`, but for one bug's
lifecycle: the current hypothesis status, its evidence chain (each entry tagged
with its fixed scoring type and FOR/AGAINST direction), and the ordered state
transitions. It performs no state writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import MisconceptionStatus
from ..domain.models import Evidence, MisconceptionHypothesis, StateTransition
from ..engine.misconception.scoring import scoring_type
from ..storage.protocols import Repository


@dataclass
class EvidenceTraceItem:
    evidence_id: str
    evidence_type: str
    result: str | None
    scoring_type: str
    direction: str | None  # FOR / AGAINST / None
    strength: str | None
    task_id: str
    scenario_fingerprint: str | None
    high_discrimination: bool
    occurred_at: str


@dataclass
class MisconceptionTrace:
    bug_id: str
    status: str
    evidence_score: int
    confidence_band: str
    hypothesis_group: str | None
    changed_task_pass_count: int
    changed_task_pass_fingerprints: list[str] = field(default_factory=list)
    hypothesis_cycle: int = 0
    remediation_version: int | None = None
    evidence_chain: list[EvidenceTraceItem] = field(default_factory=list)
    transitions: list[dict] = field(default_factory=list)


def _direction_of(evidence: Evidence, bug_id: str) -> str | None:
    for s in evidence.misconception_signals:
        if s.bug_id == bug_id:
            return s.direction.value
    return None


def _strength_of(evidence: Evidence, bug_id: str) -> str | None:
    for s in evidence.misconception_signals:
        if s.bug_id == bug_id:
            return s.strength.value
    return None


def build_trace(
    repo: Repository,
    project_id: str,
    bug_id: str,
) -> MisconceptionTrace:
    """Build the lifecycle trace for one bug. Read-only."""
    mis = repo.get_misconception(project_id, bug_id)
    evidence = repo.evidence_for_misconception(project_id, bug_id)
    # Order evidence newest-first for display.
    evidence_sorted = sorted(evidence, key=lambda e: e.occurred_at, reverse=True)

    chain = [
        EvidenceTraceItem(
            evidence_id=e.evidence_id,
            evidence_type=e.evidence_type.value,
            result=e.result.value if e.result else None,
            scoring_type=scoring_type(e),
            direction=_direction_of(e, bug_id),
            strength=_strength_of(e, bug_id),
            task_id=e.task_id,
            scenario_fingerprint=e.scenario_fingerprint,
            high_discrimination=e.high_discrimination,
            occurred_at=e.occurred_at.isoformat(),
        )
        for e in evidence_sorted
    ]

    transitions = [
        {
            "transition_id": t.transition_id,
            "old_state": t.old_state,
            "new_state": t.new_state,
            "triggering_evidence_id": t.triggering_evidence_id,
            "rule_version": t.rule_version,
            "created_at": t.created_at.isoformat(),
        }
        for t in repo.transitions_for(
            project_id, entity_type="misconception", entity_id=bug_id,
        )
    ]

    if mis is None:
        # No hypothesis record yet — return an empty trace for the bug id.
        return MisconceptionTrace(
            bug_id=bug_id, status=MisconceptionStatus.SUSPECTED.value,
            evidence_score=0, confidence_band="LOW", hypothesis_group=None,
            changed_task_pass_count=0, evidence_chain=chain, transitions=transitions,
        )

    return MisconceptionTrace(
        bug_id=bug_id,
        status=mis.status.value,
        evidence_score=mis.evidence_score,
        confidence_band=mis.confidence_band.value,
        hypothesis_group=mis.hypothesis_group,
        changed_task_pass_count=mis.changed_task_pass_count,
        changed_task_pass_fingerprints=list(mis.changed_task_pass_fingerprints),
        hypothesis_cycle=mis.hypothesis_cycle,
        remediation_version=mis.remediation_version,
        evidence_chain=chain,
        transitions=transitions,
    )


def project_traces(repo: Repository, project_id: str) -> list[MisconceptionTrace]:
    """Build a trace for every misconception in the project."""
    return [build_trace(repo, project_id, m.bug_id) for m in repo.all_misconceptions(project_id)]
