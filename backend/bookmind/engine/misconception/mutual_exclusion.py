"""Mutual-exclusion enforcement for competing hypotheses — LEARNING_MODEL.md §8.

"同一 ``hypothesis_group`` 内的候选可以同时处于 SUSPECTED，但一次探针只能把一个
最匹配假设向 CONFIRMED 推进，并为被排除假设写入 AGAINST Evidence；互斥假设不得
同时 CONFIRMED。"

The pure :mod:`~bookmind.engine.misconception.state_machine` reasons about one
hypothesis at a time and cannot see its siblings. This module is the Engine-
layer invariant that, after a state update, demotes any extra CONFIRMED
hypotheses in a group so that at most one remains. It does not delete Evidence;
it only adjusts ``status`` and records a :class:`StateTransition`.
"""

from __future__ import annotations

import uuid

from ...domain.enums import MisconceptionStatus
from ...domain.models import MisconceptionHypothesis, StateTransition
from ...storage.protocols import Repository

RULE_VERSION = "mutex_v1"


def enforce_group(
    repo: Repository,
    project_id: str,
    group: str,
) -> list[StateTransition]:
    """Ensure at most one CONFIRMED hypothesis in ``group`` for ``project_id``.

    Returns the list of demotion transitions that were recorded. Idempotent: a
    group with zero or one CONFIRMED hypothesis is a no-op (returns ``[]``).
    """
    members = [
        m for m in repo.all_misconceptions(project_id)
        if m.hypothesis_group == group and m.status == MisconceptionStatus.CONFIRMED
    ]
    if len(members) <= 1:
        return []

    # Keep the strongest (highest evidence_score), tie-break by bug_id asc for
    # determinism — the lexicographically smallest bug_id wins.
    members.sort(key=lambda m: (-m.evidence_score, m.bug_id))
    keeper = members[0]
    demotions = members[1:]

    transitions: list[StateTransition] = []
    for sib in demotions:
        new_status = _demote_to(sib)
        updated = sib.model_copy(update={"status": new_status})
        repo.upsert_misconception(updated)
        t = StateTransition(
            transition_id=str(uuid.uuid4()),
            entity_type="misconception",
            entity_id=sib.bug_id,
            project_id=project_id,
            old_state=MisconceptionStatus.CONFIRMED.value,
            new_state=new_status.value,
            triggering_evidence_id=None,
            rule_version=RULE_VERSION,
        )
        repo.record_transition(t)
        transitions.append(t)
    return transitions


def enforce_for_bugs(
    repo: Repository,
    project_id: str,
    bug_ids: list[str],
) -> list[StateTransition]:
    """Enforce mutual exclusion for every group touched by ``bug_ids``.

    Looks up each bug's ``hypothesis_group`` and runs :func:`enforce_group` once
    per distinct group. Groups that are None (no competing set) are skipped.
    """
    from ...agents.bug_library import BUG_LIBRARY

    groups: set[str | None] = set()
    for bid in bug_ids:
        mis = repo.get_misconception(project_id, bid)
        if mis is not None and mis.hypothesis_group is not None:
            groups.add(mis.hypothesis_group)
        # A bug may be hypothesis-grouped even before its MisconceptionHypothesis
        # exists; fall back to the BugLibrary if the bug carries a group hint.
        # (BugEntry itself has no group field; groups are assigned by the
        # service layer when it seeds competing hypotheses. So we rely on the
        # stored MisconceptionHypothesis here.)

    transitions: list[StateTransition] = []
    for group in groups:
        if group is None:
            continue
        transitions.extend(enforce_group(repo, project_id, group))
    return transitions


def _demote_to(hypothesis: MisconceptionHypothesis) -> MisconceptionStatus:
    """The status a demoted CONFIRMED hypothesis falls back to, by its score.

    LIKELY needs score >= 4; otherwise SUSPECTED (the floor). We never demote
    straight to DISMISSED — that requires an explicit disproof, not just a
    stronger sibling.
    """
    if hypothesis.evidence_score >= 4:
        return MisconceptionStatus.LIKELY
    return MisconceptionStatus.SUSPECTED
