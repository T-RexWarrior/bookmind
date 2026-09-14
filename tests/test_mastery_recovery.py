"""L1 tests: mastery recovery invariants — LEARNING_MODEL §5 invariants 3–5.

Pins:
  - Invariant 3: a lapsed lower level blocks higher levels (derived), without
    deleting higher-level history.
  - Invariant 4: a high-order task whose rubric covers the lower standard can
    *restore* a blocked lower level in one transaction; a high-order task that
    does NOT declare evidence for the lower level cannot.
  - "旧 PASS 不阻止当前状态更新": an old PASS does not freeze the current
    level — a later independent FAIL still moves current to UNSTABLE.
  - "低等级过期时高等级进入派生阻断而不删除历史 Evidence": expiry of L1
    demotes current_verified_level but L2's raw Evidence/record persists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bookmind.domain.enums import (
    ActivityMode,
    EvidenceResult,
    InterventionPolicy,
    JudgmentStatus,
    Level,
    LevelStatus,
    UIPreset,
)
from bookmind.domain.models import (
    AnswerJudgment,
    Book,
    Concept,
    InteractionContext,
    LearningProject,
    ProjectBook,
    ReviewPolicy,
    TrustedTaskContext,
    User,
)
from bookmind.domain.enums import BookRole
from bookmind.engine.learning_engine import submit_answer
from bookmind.engine.mastery.state import derived_effective_status, recompute
from bookmind.storage.in_memory import InMemoryRepository

from bookmind.domain.enums import DerivedEffectiveStatus


def _repo():
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="Java"))
    repo.add_book(Book(book_id="b1", owner_user_id="u1", source_hash="h", title="Java"))
    repo.link_book(ProjectBook(project_id="p1", book_id="b1", role=BookRole.PRIMARY))
    repo.add_concept(Concept(concept_id="c1", book_id="b1", name="c1", importance=0.8, goal_relevance=0.8))
    return repo


def _interaction():
    return InteractionContext(
        activity_mode=ActivityMode.READING, intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
    )


def _submit(repo, *, task, result=EvidenceResult.PASS, submission_id, judgment=None):
    j = judgment or AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=result)
    return submit_answer(
        repo, learner_id="u1", project_id="p1", task=task, interaction=_interaction(),
        judgment=j, answer_text="a", policy=ReviewPolicy(), submission_id=submission_id,
        evidence_id=f"e{submission_id}", source_book_id="b1",
    )


def _task(levels, task_id="t1"):
    return TrustedTaskContext(
        task_id=task_id, task_version=1, target_concept_ids=["c1"],
        evidence_for_levels=list(levels), rubric=["x"],
    )


# --- Invariant 4: high-order task restores blocked lower level ------------

def test_high_order_task_covering_lower_restores_blocked_lower():
    """If L1 has expired and an L2 task declares evidence_for_levels=[L1,L2]
    (rubric covers L1), passing it restores L1 AND verifies L2 in one
    transaction — LEARNING_MODEL §5 invariant 4."""
    repo = _repo()
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    # Verify L1 first.
    _submit(repo, task=_task([Level.L1], "t1"), submission_id="s1")
    # Force L1 to be verified at t0 with small stability so it expires soon.
    state = repo.get_state("p1", "c1")
    rec = state.level_record(Level.L1)
    state.set_level_record(Level.L1, rec.model_copy(update={"verified_at": t0, "stability_days": 2.0}))
    repo.save_state(state)

    far = t0 + timedelta(days=30)  # L1 expired by now
    # An L2 task whose rubric covers L1 (evidence_for_levels=[L1,L2]).
    from bookmind.domain.models import utcnow
    # Patch utcnow so the new evidence occurs at `far`.
    import bookmind.domain.models as models
    orig = models.utcnow
    models.utcnow = lambda: far
    try:
        _submit(repo, task=_task([Level.L1, Level.L2], "t2"), submission_id="s2")
    finally:
        models.utcnow = orig

    state = repo.get_state("p1", "c1")
    # L1 restored to VERIFIED, L2 verified, current = L2.
    assert state.level_record(Level.L1).status == LevelStatus.VERIFIED
    assert state.level_record(Level.L2).status == LevelStatus.VERIFIED
    assert state.current_verified_level == Level.L2


def test_high_order_task_not_covering_lower_cannot_restore():
    """An L2 task that declares only [L2] cannot restore an expired L1 — L1
    stays expired, so L2 is derived BLOCKED even though it passed the gate."""
    repo = _repo()
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    _submit(repo, task=_task([Level.L1], "t1"), submission_id="s1")
    state = repo.get_state("p1", "c1")
    rec = state.level_record(Level.L1)
    state.set_level_record(Level.L1, rec.model_copy(update={"verified_at": t0, "stability_days": 2.0}))
    repo.save_state(state)

    far = t0 + timedelta(days=30)
    import bookmind.domain.models as models
    orig = models.utcnow
    models.utcnow = lambda: far
    try:
        _submit(repo, task=_task([Level.L2], "t2"), submission_id="s2")
    finally:
        models.utcnow = orig

    state = repo.get_state("p1", "c1")
    # L2 record is VERIFIED (its own evidence passed), but L1 is still EXPIRED.
    assert state.level_record(Level.L1).status == LevelStatus.EXPIRED
    assert state.level_record(Level.L2).status == LevelStatus.VERIFIED
    # current cannot exceed the contiguous run → L0 (L1 is the gap).
    assert state.current_verified_level == Level.L0
    # L2 is derived BLOCKED_BY_LOWER_LEVEL.
    assert derived_effective_status(Level.L2, state) == DerivedEffectiveStatus.BLOCKED_BY_LOWER_LEVEL


# --- Old PASS does not freeze current state -------------------------------

def test_old_pass_does_not_block_current_fail_update():
    """A prior PASS does not prevent a later independent FAIL from moving the
    current level to UNSTABLE — PRODUCT_SPEC §8 "旧 PASS 不阻止当前状态更新"."""
    repo = _repo()
    _submit(repo, task=_task([Level.L1], "t1"), submission_id="s_pass")
    assert repo.get_state("p1", "c1").current_verified_level == Level.L1
    # Later independent FAIL at L1.
    _submit(repo, task=_task([Level.L1], "t2"), result=EvidenceResult.FAIL, submission_id="s_fail")
    rec = repo.get_state("p1", "c1").level_record(Level.L1)
    assert rec.status == LevelStatus.UNSTABLE


# --- Expiry demotes current but preserves higher history ------------------

def test_expiry_demotes_current_preserves_higher_history():
    """When L1 expires, current_verified_level falls to L0 but L2's raw record
    and Evidence persist (not deleted) — §5 invariant 1+3."""
    repo = _repo()
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    # Verify L1 and L2 together.
    _submit(repo, task=_task([Level.L1, Level.L2], "t1"), submission_id="s1")
    state = repo.get_state("p1", "c1")
    assert state.current_verified_level == Level.L2

    # Force both to be verified at t0 with small stability so L1 expires first.
    for lvl, s in ((Level.L1, 2.0), (Level.L2, 4.0)):
        rec = state.level_record(lvl)
        state.set_level_record(lvl, rec.model_copy(update={"verified_at": t0, "stability_days": s}))
    repo.save_state(state)

    # At t0+3d: L1 (S=2) expired, L2 (S=4) not yet expired.
    out = recompute(repo.get_state("p1", "c1"), t0 + timedelta(days=3), ReviewPolicy())
    assert out.current_verified_level == Level.L0  # L1 gap → current falls
    # L2 raw record preserved (VERIFIED), just derived-blocked.
    assert out.level_record(Level.L2).status == LevelStatus.VERIFIED
    assert derived_effective_status(Level.L2, out) == DerivedEffectiveStatus.BLOCKED_BY_LOWER_LEVEL
    # Evidence not deleted.
    assert len(repo.evidence_for("p1", "c1")) >= 1
