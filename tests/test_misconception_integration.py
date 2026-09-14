"""Phase 5 integration tests: end-to-end misconception closure through submit_answer.

Drives the full closed loop with the real Learning Engine transaction, the probe
classifier wiring, and mutual-exclusion enforcement. This is the acceptance
gate: "高区分度探针区分核心假设且互斥假设不能同时 CONFIRMED" and "状态可以到达
RESOLVED、处理 changed task 失败并支持复发" exercised end-to-end.
"""

from __future__ import annotations

import uuid

from bookmind.agents.bug_library import BUG_LIBRARY
from bookmind.domain.enums import (
    ActivityMode, EvidenceResult, EvidenceType, InterventionPolicy,
    JudgmentStatus, Level, MisconceptionStatus, SignalDirection, SignalStrength, UIPreset,
)
from bookmind.domain.models import (
    AnswerJudgment, Book, InteractionContext, LearningProject, MisconceptionHypothesis,
    MisconceptionSignal, ProjectBook, ReviewPolicy, TrustedTaskContext, User,
)
from bookmind.engine.learning_engine import submit_answer
from bookmind.storage.in_memory import InMemoryRepository


def _setup():
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1", display_name="Ada"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="Java"))
    from bookmind.domain.enums import BookRole
    repo.add_book(Book(book_id="b1", owner_user_id="u1", source_hash="b1", title="Java"))
    repo.link_book(ProjectBook(project_id="p1", book_id="b1", role=BookRole.PRIMARY))
    from bookmind.agents.concept_skeleton import build_skeleton
    for c in build_skeleton("b1"):
        repo.add_concept(c)
    return repo


def _interaction():
    return InteractionContext(
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING, hints_issued=0,
    )


def _submit(repo, task, judgment, answer_text, sub_id):
    return submit_answer(
        repo, learner_id="u1", project_id="p1", task=task, interaction=_interaction(),
        judgment=judgment, answer_text=answer_text, policy=ReviewPolicy(),
        submission_id=sub_id, evidence_id=f"e{uuid.uuid4().hex[:6]}", source_book_id="b1",
    )


def _probe_task(bug_id):
    bug = BUG_LIBRARY[bug_id]
    return TrustedTaskContext(
        task_id=f"probe|{bug_id}|{uuid.uuid4().hex[:4]}", task_version=1,
        target_concept_ids=list(bug.related_concepts),
        evidence_for_levels=[Level.L2], rubric=list(bug.rubric),
        is_probe=True, discriminated_bug_ids=[bug_id],
    )


def _verify_task(bug_id, tid, with_signal=True):
    bug = BUG_LIBRARY[bug_id]
    return TrustedTaskContext(
        task_id=tid, task_version=1, target_concept_ids=list(bug.related_concepts),
        evidence_for_levels=[Level.L2], rubric=list(bug.rubric),
    )


# --- probe classifier wiring inside submit_answer -------------------------

def test_probe_with_no_explicit_signals_uses_classifier():
    """A probe submission with empty signals auto-classifies the answer text."""
    repo = _setup()
    bug_id = "bug_ref_vs_object"
    # Submit a value-semantics wrong answer as a probe with NO signals.
    task = _probe_task(bug_id)
    judgment = AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.FAIL)
    _submit(repo, task, judgment, "a.getValue() returns the original value because b is a separate copy.", "s1")
    mis = repo.get_misconception("p1", bug_id)
    assert mis is not None
    # The classifier wrote a FOR signal, so the bug is at least SUSPECTED.
    assert mis.evidence_score >= 1
    # The evidence carries a FOR signal from the classifier.
    ev = repo.evidence_for_misconception("p1", bug_id)
    assert any(s.direction.value == "FOR" for e in ev for s in e.misconception_signals)


def test_one_error_does_not_confirm_misconception():
    """A single probe FOR signal must not reach CONFIRMED (needs score>=6)."""
    repo = _setup()
    bug_id = "bug_ref_vs_object"
    task = _probe_task(bug_id)
    judgment = AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.FAIL)
    _submit(repo, task, judgment, "b is a copy so a keeps the original value.", "s1")
    mis = repo.get_misconception("p1", bug_id)
    assert mis.status != MisconceptionStatus.CONFIRMED


# --- mutual exclusion end-to-end ------------------------------------------

def test_mutual_exclusion_prevents_double_confirmed():
    """Two hypotheses in the same group both reaching CONFIRMED → one demoted."""
    repo = _setup()
    # Seed two CONFIRMED hypotheses in the same group directly, then trigger
    # enforcement via a third submission that touches one of them.
    repo.upsert_misconception(MisconceptionHypothesis(
        project_id="p1", bug_id="bug_a", hypothesis_group="g1",
        status=MisconceptionStatus.CONFIRMED, evidence_score=8,
        evidence_ids=["e_a1", "e_a2", "e_a3"],
    ))
    repo.upsert_misconception(MisconceptionHypothesis(
        project_id="p1", bug_id="bug_b", hypothesis_group="g1",
        status=MisconceptionStatus.CONFIRMED, evidence_score=6,
        evidence_ids=["e_b1", "e_b2", "e_b3"],
    ))
    # Add evidence so evidence_for_misconception returns non-empty for both.
    from bookmind.domain.models import Evidence
    for bid, eids in [("bug_a", ["e_a1", "e_a2", "e_a3"]), ("bug_b", ["e_b1", "e_b2", "e_b3"])]:
        for eid in eids:
            repo.append_evidence(Evidence(
                evidence_id=eid, event_key=f"k_{eid}", project_id="p1",
                concept_id="c_reference", source_book_id="b1",
                evidence_type=EvidenceType.PROBE, required_level="L2",
                result=EvidenceResult.FAIL, independent=True, task_id=f"t_{eid}",
                misconception_signals=[MisconceptionSignal(bug_id=bid,
                    direction=SignalDirection.FOR, strength=SignalStrength.STRONG)],
                high_discrimination=True,
            ))
    # Submit a new probe answer for bug_a — this triggers enforce_for_bugs
    # on bug_a's group, demoting bug_b.
    task = _probe_task("bug_ref_vs_object")  # any probe; we'll attach signal to bug_a
    judgment = AnswerJudgment(
        judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.FAIL,
        misconception_signals=[MisconceptionSignal(bug_id="bug_a",
            direction=SignalDirection.FOR, strength=SignalStrength.MEDIUM)],
    )
    _submit(repo, task, judgment, "wrong", "s_trigger")
    # After enforcement, only one of bug_a/bug_b is CONFIRMED.
    confirmed = [m for m in repo.all_misconceptions("p1")
                 if m.status == MisconceptionStatus.CONFIRMED and m.bug_id in ("bug_a", "bug_b")]
    assert len(confirmed) == 1
    assert confirmed[0].bug_id == "bug_a"  # higher score kept


# --- full closure with relapse --------------------------------------------

def test_full_closure_confirmed_to_resolved_to_relapse():
    repo = _setup()
    bug_id = "bug_ref_vs_object"

    # 1. Three FOR evidences from distinct tasks (incl. one high-disc probe) → CONFIRMED.
    for i, (tid, etype, high) in enumerate([
        ("t1", EvidenceType.VERIFY, False),
        ("t2", EvidenceType.VERIFY, False),
        ("t3", EvidenceType.PROBE, True),
    ]):
        task = TrustedTaskContext(
            task_id=tid, task_version=1, target_concept_ids=["c_reference"],
            evidence_for_levels=[Level.L2], rubric=BUG_LIBRARY[bug_id].rubric,
            is_probe=(etype == EvidenceType.PROBE),
            discriminated_bug_ids=[bug_id] if etype == EvidenceType.PROBE else [],
        )
        judgment = AnswerJudgment(
            judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.FAIL,
            misconception_signals=[MisconceptionSignal(bug_id=bug_id,
                direction=SignalDirection.FOR, strength=SignalStrength.STRONG)],
        )
        _submit(repo, task, judgment, f"wrong {i}", f"cf{i}")
    assert repo.get_misconception("p1", bug_id).status == MisconceptionStatus.CONFIRMED

    # 2. Start remediation.
    from bookmind.engine.learning_engine import start_remediation
    start_remediation(repo, project_id="p1", bug_id=bug_id)
    assert repo.get_misconception("p1", bug_id).status == MisconceptionStatus.REMEDIATING

    # 3. Two changed-task PASS with distinct fingerprints → RESOLVED.
    for stage, fp in [(1, "scene_X"), (2, "scene_Y")]:
        task = TrustedTaskContext(
            task_id=f"ct{stage}", task_version=1, target_concept_ids=["c_reference"],
            evidence_for_levels=[Level.L3], rubric=BUG_LIBRARY[bug_id].rubric,
            is_changed_task=True, discriminated_bug_ids=[bug_id],
            scenario_fingerprint=fp, remediation_stage=stage,
        )
        judgment = AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.PASS)
        _submit(repo, task, judgment, f"correct {stage}", f"ctp{stage}")
    assert repo.get_misconception("p1", bug_id).status == MisconceptionStatus.RESOLVED

    # 4. New high-disc probe → RELAPSED.
    task = _probe_task(bug_id)
    judgment = AnswerJudgment(
        judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.FAIL,
        misconception_signals=[MisconceptionSignal(bug_id=bug_id,
            direction=SignalDirection.FOR, strength=SignalStrength.STRONG)],
    )
    _submit(repo, task, judgment, "wrong again", "relapse1")
    assert repo.get_misconception("p1", bug_id).status == MisconceptionStatus.RELAPSED


def test_changed_task_fail_during_remediation_back_to_confirmed():
    repo = _setup()
    bug_id = "bug_ref_vs_object"
    for i in range(3):
        task = TrustedTaskContext(
            task_id=f"p{i}", task_version=1, target_concept_ids=["c_reference"],
            evidence_for_levels=[Level.L2], rubric=BUG_LIBRARY[bug_id].rubric,
            is_probe=(i == 2), discriminated_bug_ids=[bug_id] if i == 2 else [],
        )
        judgment = AnswerJudgment(
            judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.FAIL,
            misconception_signals=[MisconceptionSignal(bug_id=bug_id,
                direction=SignalDirection.FOR, strength=SignalStrength.STRONG)],
        )
        _submit(repo, task, judgment, f"wrong {i}", f"cf{i}")
    from bookmind.engine.learning_engine import start_remediation
    start_remediation(repo, project_id="p1", bug_id=bug_id)
    assert repo.get_misconception("p1", bug_id).status == MisconceptionStatus.REMEDIATING
    # FAIL a changed task → back to CONFIRMED.
    task = TrustedTaskContext(
        task_id="ct_fail", task_version=1, target_concept_ids=["c_reference"],
        evidence_for_levels=[Level.L3], rubric=BUG_LIBRARY[bug_id].rubric,
        is_changed_task=True, discriminated_bug_ids=[bug_id],
        scenario_fingerprint="scene_FAIL", remediation_stage=1,
    )
    judgment = AnswerJudgment(
        judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.FAIL,
        misconception_signals=[MisconceptionSignal(bug_id=bug_id,
            direction=SignalDirection.FOR, strength=SignalStrength.MEDIUM)],
    )
    _submit(repo, task, judgment, "still wrong", "ctfail1")
    assert repo.get_misconception("p1", bug_id).status == MisconceptionStatus.CONFIRMED
    assert repo.get_misconception("p1", bug_id).changed_task_pass_count == 0
