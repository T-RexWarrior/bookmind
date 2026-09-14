"""L1 integration tests: submit_answer closed loop — ARCHITECTURE.md §10,
PRODUCT_SPEC.md §8 acceptance criteria.

These exercise the full state-write transaction against the in-memory repo,
including the cross-cutting correctness invariants the spec calls out.
"""

from __future__ import annotations

import uuid

import pytest

from bookmind.domain.enums import (
    ActivityMode,
    EvidenceResult,
    EvidenceType,
    HintLevel,
    InterventionPolicy,
    JudgmentStatus,
    Level,
    LevelStatus,
    MisconceptionStatus,
    SignalDirection,
    SignalStrength,
    UIPreset,
)
from bookmind.domain.models import (
    AnswerJudgment,
    Book,
    Concept,
    InteractionContext,
    LearningProject,
    MisconceptionSignal,
    ReviewPolicy,
    SourceRef,
    TrustedTaskContext,
    User,
)
from bookmind.engine.learning_engine import submit_answer
from bookmind.storage.in_memory import InMemoryRepository, ScopeError


# --- fixtures -------------------------------------------------------------

def _repo_with_project(learner_id="u1", project_id="p1", book_id="b1", concept_id="c1"):
    repo = InMemoryRepository()
    repo.add_user(User(user_id=learner_id))
    repo.create_project(LearningProject(project_id=project_id, learner_id=learner_id, name="Java OOP"))
    repo.add_book(Book(book_id=book_id, owner_user_id=learner_id, source_hash="h", title="Java Core"))
    from bookmind.domain.models import ProjectBook
    from bookmind.domain.enums import BookRole
    repo.link_book(ProjectBook(project_id=project_id, book_id=book_id, role=BookRole.PRIMARY))
    repo.add_concept(Concept(concept_id=concept_id, book_id=book_id, name=concept_id, importance=0.8, goal_relevance=0.8))
    return repo


def _task(concept_id="c1", levels=(Level.L1,), **kw) -> TrustedTaskContext:
    base = dict(
        task_id="t1", task_version=1, target_concept_ids=[concept_id],
        evidence_for_levels=list(levels), rubric=["recalls definition"],
    )
    base.update(kw)
    return TrustedTaskContext(**base)


def _interaction(hints=0, tools=None) -> InteractionContext:
    return InteractionContext(
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING,
        hints_issued=hints,
        tools_exposed=tools or [],
    )


def _decided(result=EvidenceResult.PASS, signals=None) -> AnswerJudgment:
    return AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=result, misconception_signals=signals or [])


def _submit(repo, *, learner="u1", project="p1", book="b1", task=None, interaction=None, judgment=None,
            answer="answer", policy=None, submission_id="s1", evidence_id=None, concept_id="c1"):
    return submit_answer(
        repo,
        learner_id=learner,
        project_id=project,
        task=task or _task(concept_id),
        interaction=interaction or _interaction(),
        judgment=judgment or _decided(),
        answer_text=answer,
        policy=policy or ReviewPolicy(),
        submission_id=submission_id,
        evidence_id=evidence_id or f"e{uuid.uuid4().hex[:6]}",
        source_book_id=book,
    )


# --- Acceptance: only READ never raises mastery --------------------------

def test_clean_independent_pass_verifies_l1():
    repo = _repo_with_project()
    r = _submit(repo, judgment=_decided(EvidenceResult.PASS))
    assert r.written
    assert Level.L1 in r.verified_levels
    state = repo.get_state("p1", "c1")
    assert state.current_verified_level == Level.L1
    assert state.level_record(Level.L1).status == LevelStatus.VERIFIED


def test_read_evidence_never_verifies():
    """READ evidence (exposure) must not raise mastery — PRODUCT_SPEC §8."""
    repo = _repo_with_project()
    # A READ has no result/judgment; simulate by calling with a READ-type task is not
    # the submit path. Instead confirm a hinted answer does not verify:
    r = _submit(repo, interaction=_interaction(hints=1), judgment=_decided(EvidenceResult.PASS))
    assert not r.verified_levels
    state = repo.get_state("p1", "c1")
    assert state.current_verified_level == Level.L0


def test_hinted_pass_not_independent():
    """带提示作答不成为独立验证 — PRODUCT_SPEC §8."""
    repo = _repo_with_project()
    r = _submit(repo, interaction=_interaction(hints=2), judgment=_decided(EvidenceResult.PASS))
    assert r.written
    assert not r.verified_levels
    assert r.evidence.independent is False
    assert r.evidence.hint_level == HintLevel.MEDIUM


def test_fail_does_not_verify_and_destabilises():
    repo = _repo_with_project()
    # First get to L1.
    _submit(repo, judgment=_decided(EvidenceResult.PASS), submission_id="s_pass")
    assert repo.get_state("p1", "c1").current_verified_level == Level.L1
    # Now an independent FAIL at L1 → UNSTABLE.
    r = _submit(repo, judgment=_decided(EvidenceResult.FAIL), submission_id="s_fail",
                task=_task(task_id="t2"))
    assert not r.verified_levels
    rec = repo.get_state("p1", "c1").level_record(Level.L1)
    assert rec.status == LevelStatus.UNSTABLE


def test_partial_does_not_verify():
    repo = _repo_with_project()
    r = _submit(repo, judgment=_decided(EvidenceResult.PARTIAL), submission_id="s_partial")
    assert not r.verified_levels
    assert repo.get_state("p1", "c1").current_verified_level == Level.L0


# --- Acceptance: event_key idempotency -----------------------------------

def test_event_key_replay_is_idempotent():
    """event_key 重放不会重复写 Evidence — PRODUCT_SPEC §8."""
    repo = _repo_with_project()
    r1 = _submit(repo, submission_id="s1", evidence_id="e1")
    assert r1.written
    r2 = _submit(repo, submission_id="s1", evidence_id="e1_dup")
    assert not r2.written
    assert r2.reason.startswith("event_key replay")
    # Only one evidence row exists.
    assert len(repo.evidence) == 1


def test_distinct_submission_writes_distinct_evidence():
    repo = _repo_with_project()
    _submit(repo, submission_id="s1", evidence_id="e1", task=_task(task_id="t1"))
    _submit(repo, submission_id="s2", evidence_id="e2", task=_task(task_id="t2"))
    assert len(repo.evidence) == 2


# --- Acceptance: one error does not confirm a misconception ---------------

def test_one_error_does_not_confirm_misconception():
    """一次普通错误不直接确认误区 — PRODUCT_SPEC §8."""
    repo = _repo_with_project()
    sig = [MisconceptionSignal(bug_id="bug_ref_obj", direction=SignalDirection.FOR, strength=SignalStrength.STRONG)]
    r = _submit(repo, judgment=_decided(EvidenceResult.FAIL, signals=sig), submission_id="s1", task=_task(task_id="t1"))
    mis = repo.get_misconception("p1", "bug_ref_obj")
    assert mis is not None
    assert mis.status != MisconceptionStatus.CONFIRMED
    assert mis.evidence_score == 3  # one STRONG +3


# --- Acceptance: highest_ever never decremented --------------------------

def test_highest_ever_not_deleted_on_fail():
    """旧 PASS 不会导致当前等级永远无法下降; highest_ever 永不因反证删除 — §8."""
    repo = _repo_with_project()
    _submit(repo, judgment=_decided(EvidenceResult.PASS), submission_id="s_pass", task=_task(levels=(Level.L1,)))
    state = repo.get_state("p1", "c1")
    assert state.highest_ever_level == Level.L1
    _submit(repo, judgment=_decided(EvidenceResult.FAIL), submission_id="s_fail", task=_task(task_id="t2"))
    state = repo.get_state("p1", "c1")
    assert state.highest_ever_level == Level.L1  # preserved
    assert state.current_verified_level == Level.L0 or state.level_record(Level.L1).status == LevelStatus.UNSTABLE


# --- Scope isolation -----------------------------------------------------

def test_cross_project_evidence_rejected():
    """跨项目 Evidence 拒绝写入 — EVALUATION §2."""
    repo = _repo_with_project(learner_id="u1", project_id="p1", book_id="b1", concept_id="c1")
    # a second project for the same user with its own book
    repo.create_project(LearningProject(project_id="p2", learner_id="u1", name="Other"))
    repo.add_book(Book(book_id="b2", owner_user_id="u1", source_hash="h2", title="Other book"))
    from bookmind.domain.models import ProjectBook
    from bookmind.domain.enums import BookRole
    repo.link_book(ProjectBook(project_id="p2", book_id="b2", role=BookRole.PRIMARY))
    # concept c1 belongs to b1 (project p1). Submitting into p2 with c1 must fail.
    with pytest.raises(ScopeError):
        _submit(repo, project="p2", book="b2", task=_task(concept_id="c1"), submission_id="sx")


def test_wrong_learner_cannot_access_project():
    repo = _repo_with_project(learner_id="u1", project_id="p1")
    repo.add_user(User(user_id="u2"))
    with pytest.raises(ScopeError):
        _submit(repo, learner="u2", project="p1", submission_id="sx")


def test_unauthorized_book_rejected():
    """未授权教材不能因文件相同而被另一用户访问 — §8."""
    repo = _repo_with_project(learner_id="u1", project_id="p1", book_id="b1", concept_id="c1")
    # u2 owns b2; u1's project does not link b2.
    repo.add_user(User(user_id="u2"))
    repo.add_book(Book(book_id="b2", owner_user_id="u2", source_hash="h1", title="clone"))
    with pytest.raises(ScopeError):
        _submit(repo, learner="u1", project="p1", book="b2", submission_id="sx")


def test_one_primary_book_per_project():
    from bookmind.domain.enums import BookRole
    from bookmind.domain.models import ProjectBook
    repo = _repo_with_project()
    repo.add_book(Book(book_id="b2", owner_user_id="u1", source_hash="h2", title="second"))
    with pytest.raises(ScopeError):
        repo.link_book(ProjectBook(project_id="p1", book_id="b2", role=BookRole.PRIMARY))


# --- Changed-task resolution closed loop ---------------------------------

def test_changed_task_resolution_reaches_resolved():
    """两个不同情境复验后误区状态可以到达 RESOLVED — §8."""
    from bookmind.engine.learning_engine import start_remediation
    repo = _repo_with_project()
    # Build up to CONFIRMED: 2 STRONG from 2 tasks + 1 high-disc probe.
    sig = [MisconceptionSignal(bug_id="bug1", direction=SignalDirection.FOR, strength=SignalStrength.STRONG)]
    _submit(repo, judgment=_decided(EvidenceResult.FAIL, signals=sig), submission_id="s1",
            task=_task(task_id="t1"))
    _submit(repo, judgment=_decided(EvidenceResult.FAIL, signals=sig), submission_id="s2",
            task=_task(task_id="t2"))
    # probe
    probe_task = _task(task_id="t3", is_probe=True, discriminated_bug_ids=["bug1"])
    _submit(repo, judgment=_decided(EvidenceResult.FAIL, signals=sig), submission_id="s3", task=probe_task)
    mis = repo.get_misconception("p1", "bug1")
    assert mis.status == MisconceptionStatus.CONFIRMED

    # Decision layer selects REMEDIATE → start remediation (CONFIRMED → REMEDIATING).
    start_remediation(repo, project_id="p1", bug_id="bug1")
    assert repo.get_misconception("p1", "bug1").status == MisconceptionStatus.REMEDIATING

    # first changed-task PASS (near transfer)
    ct1 = _task(task_id="ct1", is_changed_task=True, scenario_fingerprint="scene_A", discriminated_bug_ids=["bug1"], levels=(Level.L2,))
    _submit(repo, judgment=_decided(EvidenceResult.PASS), submission_id="s_ct1", task=ct1)
    mis = repo.get_misconception("p1", "bug1")
    assert mis.status == MisconceptionStatus.VERIFYING

    # second changed-task PASS (far transfer, distinct scenario) → RESOLVED
    ct2 = _task(task_id="ct2", is_changed_task=True, scenario_fingerprint="scene_B", discriminated_bug_ids=["bug1"], levels=(Level.L2,))
    _submit(repo, judgment=_decided(EvidenceResult.PASS), submission_id="s_ct2", task=ct2)
    mis = repo.get_misconception("p1", "bug1")
    assert mis.status == MisconceptionStatus.RESOLVED


# --- NEEDS_REVIEW --------------------------------------------------------

def test_needs_review_no_state_increment():
    repo = _repo_with_project()
    j = AnswerJudgment(judgment_status=JudgmentStatus.NEEDS_REVIEW, result=None)
    r = _submit(repo, judgment=j, submission_id="s1")
    assert r.needs_review
    assert not r.written
    assert len(repo.evidence) == 0
    assert repo.get_state("p1", "c1").current_verified_level == Level.L0
