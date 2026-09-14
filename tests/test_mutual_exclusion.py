"""Phase 5 tests: mutual-exclusion enforcement — LEARNING_MODEL.md §8.

Covers the "互斥假设不能同时 CONFIRMED" acceptance: when two hypotheses in the
same hypothesis_group both reach CONFIRMED, the weaker is demoted. The pure
state machine is untouched; this is an Engine-layer invariant backstop.
"""

from __future__ import annotations

import uuid

from bookmind.domain.enums import MisconceptionStatus
from bookmind.domain.models import MisconceptionHypothesis
from bookmind.engine.misconception.mutual_exclusion import enforce_group, enforce_for_bugs
from bookmind.storage.in_memory import InMemoryRepository


def _mis(bug_id, group, status=MisconceptionStatus.CONFIRMED, score=6):
    return MisconceptionHypothesis(
        project_id="p1", bug_id=bug_id, hypothesis_group=group,
        status=status, evidence_score=score,
    )


def test_no_op_when_zero_or_one_confirmed():
    repo = InMemoryRepository()
    repo.upsert_misconception(_mis("bug_a", "g1", score=6))
    t = enforce_group(repo, "p1", "g1")
    assert t == []
    assert repo.get_misconception("p1", "bug_a").status == MisconceptionStatus.CONFIRMED


def test_demotes_weaker_when_two_confirmed():
    repo = InMemoryRepository()
    repo.upsert_misconception(_mis("bug_a", "g1", score=8))
    repo.upsert_misconception(_mis("bug_b", "g1", score=6))
    t = enforce_group(repo, "p1", "g1")
    assert len(t) == 1
    a = repo.get_misconception("p1", "bug_a")
    b = repo.get_misconception("p1", "bug_b")
    assert a.status == MisconceptionStatus.CONFIRMED  # stronger kept
    assert b.status == MisconceptionStatus.LIKELY     # weaker demoted (score 6 >= 4)
    # Transition recorded with mutex rule version.
    assert t[0].rule_version == "mutex_v1"
    assert t[0].old_state == "CONFIRMED"
    assert t[0].new_state == "LIKELY"


def test_demote_to_suspected_when_score_below_likely():
    repo = InMemoryRepository()
    repo.upsert_misconception(_mis("bug_a", "g1", score=9))
    repo.upsert_misconception(_mis("bug_b", "g1", score=3))  # < 4 → SUSPECTED
    enforce_group(repo, "p1", "g1")
    assert repo.get_misconception("p1", "bug_a").status == MisconceptionStatus.CONFIRMED
    assert repo.get_misconception("p1", "bug_b").status == MisconceptionStatus.SUSPECTED


def test_tie_break_by_bug_id_ascending():
    """Equal scores: lexicographically smaller bug_id is kept."""
    repo = InMemoryRepository()
    repo.upsert_misconception(_mis("bug_b", "g1", score=7))
    repo.upsert_misconception(_mis("bug_a", "g1", score=7))
    enforce_group(repo, "p1", "g1")
    assert repo.get_misconception("p1", "bug_a").status == MisconceptionStatus.CONFIRMED
    assert repo.get_misconception("p1", "bug_b").status == MisconceptionStatus.LIKELY


def test_demotion_does_not_delete_evidence_or_transitions():
    repo = InMemoryRepository()
    repo.upsert_misconception(_mis("bug_a", "g1", score=8))
    repo.upsert_misconception(_mis("bug_b", "g1", score=6))
    enforce_group(repo, "p1", "g1")
    # A transition is recorded; the demoted hypothesis still exists (not deleted).
    assert any(t.entity_id == "bug_b" for t in repo.transitions)
    assert repo.get_misconception("p1", "bug_b") is not None


def test_idempotent_repeated_call():
    """Calling enforce_group again after demotion is a no-op (only one CONFIRMED)."""
    repo = InMemoryRepository()
    repo.upsert_misconception(_mis("bug_a", "g1", score=8))
    repo.upsert_misconception(_mis("bug_b", "g1", score=6))
    t1 = enforce_group(repo, "p1", "g1")
    assert len(t1) == 1
    t2 = enforce_group(repo, "p1", "g1")
    assert t2 == []


def test_different_groups_independent():
    repo = InMemoryRepository()
    repo.upsert_misconception(_mis("bug_a", "g1", score=8))
    repo.upsert_misconception(_mis("bug_b", "g1", score=6))
    repo.upsert_misconception(_mis("bug_c", "g2", score=9))
    enforce_group(repo, "p1", "g1")
    # g2 untouched (only one member).
    assert repo.get_misconception("p1", "bug_c").status == MisconceptionStatus.CONFIRMED


def test_enforce_for_bugs_dispatches_per_group():
    repo = InMemoryRepository()
    repo.upsert_misconception(_mis("bug_a", "g1", score=8))
    repo.upsert_misconception(_mis("bug_b", "g1", score=6))
    repo.upsert_misconception(_mis("bug_c", "g2", score=9))
    repo.upsert_misconception(_mis("bug_d", "g2", score=7))
    t = enforce_for_bugs(repo, "p1", ["bug_a", "bug_c"])
    assert len(t) == 2  # one demotion per group
    assert repo.get_misconception("p1", "bug_a").status == MisconceptionStatus.CONFIRMED
    assert repo.get_misconception("p1", "bug_c").status == MisconceptionStatus.CONFIRMED


def test_group_with_none_is_skipped():
    repo = InMemoryRepository()
    repo.upsert_misconception(_mis("bug_x", None, score=9))
    t = enforce_group(repo, "p1", "g_orphan")
    assert t == []


def test_three_confirmed_keeps_one():
    repo = InMemoryRepository()
    repo.upsert_misconception(_mis("bug_a", "g1", score=5))
    repo.upsert_misconception(_mis("bug_b", "g1", score=7))
    repo.upsert_misconception(_mis("bug_c", "g1", score=6))
    t = enforce_group(repo, "p1", "g1")
    assert len(t) == 2
    confirmed = [m for m in repo.all_misconceptions("p1") if m.status == MisconceptionStatus.CONFIRMED]
    assert len(confirmed) == 1
    assert confirmed[0].bug_id == "bug_b"  # highest score
