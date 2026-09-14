"""Phase 5 tests: remediation service + end-to-end closure.

Covers the acceptance: "状态可以到达 RESOLVED、处理 changed task 失败并支持复发".
Drives the full lifecycle through the real submit_answer transaction:
CONFIRMED → REMEDIATING → (changed task PASS) → VERIFYING → (2nd PASS) → RESOLVED,
plus changed-task FAIL → CONFIRMED, and RELAPSE on new high-disc probe support.
"""

from __future__ import annotations

import uuid

from bookmind.agents.bug_library import BUG_REF_VS_OBJECT
from bookmind.domain.enums import (
    ActivityMode, EvidenceResult, EvidenceType, InterventionPolicy,
    JudgmentStatus, Level, MisconceptionStatus, SignalDirection, SignalStrength, UIPreset,
)
from bookmind.domain.models import (
    AnswerJudgment, InteractionContext, LearningProject, MisconceptionHypothesis,
    MisconceptionSignal, ReviewPolicy, TrustedTaskContext, User,
)
from bookmind.engine.learning_engine import start_remediation, submit_answer
from bookmind.engine.task.generator import generate_changed_task
from bookmind.engine.task.validator import validate
from bookmind.services.remediation import RemediationService
from bookmind.storage.in_memory import InMemoryRepository


def _setup_repo():
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1", display_name="Ada"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="Java"))
    from bookmind.domain.enums import BookRole
    from bookmind.domain.models import Book, ProjectBook
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


def _seed_confirmed(repo, bug_id="bug_ref_vs_object"):
    """Drive one bug to CONFIRMED via three FOR evidences from distinct tasks,
    including one high-discrimination probe (LEARNING_MODEL §8 thresholds)."""
    concept = "c_reference"
    for i, (tid, etype, high) in enumerate([
        ("t1", EvidenceType.VERIFY, False),
        ("t2", EvidenceType.VERIFY, False),
        ("t3", EvidenceType.PROBE, True),
    ]):
        task = TrustedTaskContext(
            task_id=tid, task_version=1, target_concept_ids=[concept],
            evidence_for_levels=[Level.L2], rubric=BUG_REF_VS_OBJECT.rubric,
            is_probe=(etype == EvidenceType.PROBE),
            discriminated_bug_ids=[bug_id] if etype == EvidenceType.PROBE else [],
        )
        judgment = AnswerJudgment(
            judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.FAIL,
            misconception_signals=[MisconceptionSignal(
                bug_id=bug_id, direction=SignalDirection.FOR, strength=SignalStrength.STRONG,
            )],
        )
        submit_answer(
            repo, learner_id="u1", project_id="p1", task=task, interaction=_interaction(),
            judgment=judgment, answer_text=f"wrong answer {i}", policy=ReviewPolicy(),
            submission_id=f"s{i}", evidence_id=f"e{i}", source_book_id="b1",
        )
    return repo.get_misconception("p1", bug_id)


# --- lifecycle ------------------------------------------------------------

def test_full_lifecycle_confirmed_to_resolved():
    repo = _setup_repo()
    mis = _seed_confirmed(repo)
    assert mis.status == MisconceptionStatus.CONFIRMED

    # Start remediation.
    svc = RemediationService(repo)
    plan = svc.start(project_id="p1", bug_id="bug_ref_vs_object")
    assert repo.get_misconception("p1", "bug_ref_vs_object").status == MisconceptionStatus.REMEDIATING
    assert plan.explanation_goal
    assert plan.positive_example
    assert plan.counterexample

    # First changed task PASS → VERIFYING.
    _submit_changed_task(repo, stage=1, result=EvidenceResult.PASS, fingerprint="scene_A")
    assert repo.get_misconception("p1", "bug_ref_vs_object").status == MisconceptionStatus.VERIFYING

    # Second changed task PASS (distinct scenario) → RESOLVED.
    _submit_changed_task(repo, stage=2, result=EvidenceResult.PASS, fingerprint="scene_B")
    assert repo.get_misconception("p1", "bug_ref_vs_object").status == MisconceptionStatus.RESOLVED


def test_changed_task_fail_back_to_confirmed():
    repo = _setup_repo()
    _seed_confirmed(repo)
    svc = RemediationService(repo)
    svc.start(project_id="p1", bug_id="bug_ref_vs_object")
    _submit_changed_task(repo, stage=1, result=EvidenceResult.PASS, fingerprint="scene_A")
    assert repo.get_misconception("p1", "bug_ref_vs_object").status == MisconceptionStatus.VERIFYING
    # A FAIL during verification resets to CONFIRMED.
    _submit_changed_task(repo, stage=2, result=EvidenceResult.FAIL, fingerprint="scene_B")
    assert repo.get_misconception("p1", "bug_ref_vs_object").status == MisconceptionStatus.CONFIRMED
    assert repo.get_misconception("p1", "bug_ref_vs_object").changed_task_pass_count == 0


def test_relapse_after_resolved():
    repo = _setup_repo()
    _seed_confirmed(repo)
    svc = RemediationService(repo)
    svc.start(project_id="p1", bug_id="bug_ref_vs_object")
    _submit_changed_task(repo, stage=1, result=EvidenceResult.PASS, fingerprint="scene_A")
    _submit_changed_task(repo, stage=2, result=EvidenceResult.PASS, fingerprint="scene_B")
    assert repo.get_misconception("p1", "bug_ref_vs_object").status == MisconceptionStatus.RESOLVED
    # New high-discrimination probe support → RELAPSED.
    task = TrustedTaskContext(
        task_id="relapse_probe", task_version=1, target_concept_ids=["c_reference"],
        evidence_for_levels=[Level.L2], rubric=BUG_REF_VS_OBJECT.rubric,
        is_probe=True, discriminated_bug_ids=["bug_ref_vs_object"],
    )
    judgment = AnswerJudgment(
        judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.FAIL,
        misconception_signals=[MisconceptionSignal(
            bug_id="bug_ref_vs_object", direction=SignalDirection.FOR, strength=SignalStrength.STRONG,
        )],
    )
    submit_answer(
        repo, learner_id="u1", project_id="p1", task=task, interaction=_interaction(),
        judgment=judgment, answer_text="wrong again", policy=ReviewPolicy(),
        submission_id="srelapse", evidence_id="erelapse", source_book_id="b1",
    )
    assert repo.get_misconception("p1", "bug_ref_vs_object").status == MisconceptionStatus.RELAPSED


def test_duplicate_scenario_does_not_resolve():
    repo = _setup_repo()
    _seed_confirmed(repo)
    svc = RemediationService(repo)
    svc.start(project_id="p1", bug_id="bug_ref_vs_object")
    _submit_changed_task(repo, stage=1, result=EvidenceResult.PASS, fingerprint="scene_A")
    # Same fingerprint → does not advance to RESOLVED.
    _submit_changed_task(repo, stage=2, result=EvidenceResult.PASS, fingerprint="scene_A")
    mis = repo.get_misconception("p1", "bug_ref_vs_object")
    assert mis.status == MisconceptionStatus.VERIFYING
    assert mis.changed_task_pass_count == 1


# --- service scope --------------------------------------------------------

def test_start_remediation_unknown_bug_raises():
    repo = _setup_repo()
    svc = RemediationService(repo)
    try:
        svc.start(project_id="p1", bug_id="bug_no_such")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for unknown bug")


def test_build_changed_task_validates():
    repo = _setup_repo()
    svc = RemediationService(repo)
    res = svc.build_changed_task(project_id="p1", bug_id="bug_ref_vs_object", stage=1)
    assert res.trusted is not None
    assert res.trusted.remediation_stage == 1
    assert res.report.passed


# --- helper ---------------------------------------------------------------

def _submit_changed_task(repo, *, stage, result, fingerprint):
    bug = BUG_REF_VS_OBJECT
    draft = generate_changed_task(bug, stage=stage, target_concept_ids=["c_reference"])
    # Override the fingerprint with the one the test wants, to control dedup.
    report = validate(draft, repo, "p1")
    assert report.passed
    trusted = report.trusted.model_copy(update={"scenario_fingerprint": fingerprint})
    judgment = AnswerJudgment(
        judgment_status=JudgmentStatus.DECIDED, result=result,
        misconception_signals=[] if result == EvidenceResult.PASS else [
            MisconceptionSignal(bug_id=bug.bug_id, direction=SignalDirection.FOR, strength=SignalStrength.MEDIUM)
        ],
    )
    submit_answer(
        repo, learner_id="u1", project_id="p1", task=trusted, interaction=_interaction(),
        judgment=judgment, answer_text=f"changed-task answer {stage}", policy=ReviewPolicy(),
        submission_id=f"cts_{stage}_{fingerprint}", evidence_id=f"cte_{uuid.uuid4().hex[:6]}",
        source_book_id="b1",
    )
