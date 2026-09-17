"""Learner-profile projection tests.

The LLM may explain evidence, but cannot manufacture a verification event or
write L1-L4 directly. These tests pin that boundary and the durable projection.
"""

from __future__ import annotations

from bookmind.domain.enums import (
    ActivityMode, BookRole, EvidenceResult, EvidenceType, HintLevel,
    InterventionPolicy, JudgmentStatus, Level, UIPreset,
)
from bookmind.domain.models import (
    AnswerJudgment, Book, Concept, Evidence, InteractionContext,
    LearningProject, ProjectBook, TrustedTaskContext, User,
)
from bookmind.engine.evidence.gate import can_verify_mastery
from bookmind.llm.schemas import ModelResult
from bookmind.api.routes.projects import learning_profile
from bookmind.services.learner_profile import (
    LearnerProfileService,
    profile_for,
    record_task_interaction_fact,
    render_profile_context,
)
from bookmind.storage.in_memory import InMemoryRepository


class _ProfileRouter:
    def complete(self, task, messages, **kwargs):
        assert task == "learner_profile_update"
        return ModelResult(
            ok=True, task=task, model="test-model", content="{}",
            parsed_json={"concept_profiles": [{
                "concept_id": "c_queue",
                "summary": "能说明入队位置，但出队顺序仍需验证。",
                "observed_understanding": ["知道新元素进入队尾"],
                "needs_attention": ["队首与队尾的删除顺序"],
                "next_practice_goal": "完成一道连续入队和出队的操作序列题。",
                "confidence": 0.84,
                "evidence_basis": ["本次独立作答为部分正确"],
            }]},
        )


def _repo():
    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="DS"))
    repo.add_book(Book(book_id="b1", owner_user_id="u1", source_hash="h", title="DS"))
    repo.link_book(ProjectBook(project_id="p1", book_id="b1", role=BookRole.PRIMARY))
    repo.add_concept(Concept(concept_id="c_queue", book_id="b1", name="队列", description="先进先出容器"))
    return repo


def _task():
    return TrustedTaskContext(
        task_id="t1", task_version=1, target_concept_ids=["c_queue"],
        evidence_for_levels=[Level.L1], rubric=["说明入队和出队的位置"],
    )


def test_profile_is_persisted_as_a_projection_of_answer_evidence():
    repo = _repo()
    task = _task()
    evidence = Evidence(
        evidence_id="e1", event_key="e1", project_id="p1", concept_id="c_queue",
        source_book_id="b1", evidence_type=EvidenceType.VERIFY,
        result=EvidenceResult.PARTIAL, independent=True, task_id="t1",
        content_summary="把出队位置写成队尾",
    )
    assert repo.append_evidence(evidence)
    profiles = LearnerProfileService(repo, _ProfileRouter()).update_after_submission(
        project_id="p1", task=task,
        interaction=InteractionContext(
            activity_mode=ActivityMode.REVIEW, intervention_policy=InterventionPolicy.PROACTIVE,
            ui_preset=UIPreset.DEEP_LEARNING,
        ),
        judgment=AnswerJudgment(
            judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.PARTIAL,
            reason="把出队位置写成队尾",
        ),
        answer_text="出队从队尾删除",
        evidence=evidence,
    )
    assert profiles[0]["concept_id"] == "c_queue"
    profile = profile_for(repo, "p1", "c_queue")
    assert profile is not None
    assert "队首与队尾" in profile["needs_attention"][0]
    assert "队列" not in render_profile_context(repo, "p1", ["missing"])
    assert "连续入队" in render_profile_context(repo, "p1", ["c_queue"])
    # The model explanation is not allowed to alter L1-L4 state directly.
    assert repo.get_state("p1", "c_queue").current_verified_level == Level.L0


def test_hint_fact_is_durable_but_cannot_pass_evidence_gate():
    repo = _repo()
    written = record_task_interaction_fact(
        repo,
        task_data={
            "project_id": "p1", "task_id": "t1", "task_version": 1,
            "target_concept_ids": ["c_queue"], "conversation_id": "conv1",
        },
        evidence_type=EvidenceType.HINT,
        detail="hint_number=1",
    )
    assert len(written) == 1
    fact = repo.evidence_for("p1", "c_queue")[0]
    decision = can_verify_mastery(fact, _task(), None)
    assert not decision.passed_gate
    assert "non-verifying" in " ".join(decision.blocks)


def test_profile_endpoint_keeps_engine_state_beside_model_interpretation():
    repo = _repo()
    task = _task()
    evidence = Evidence(
        evidence_id="e2", event_key="e2", project_id="p1", concept_id="c_queue",
        source_book_id="b1", evidence_type=EvidenceType.VERIFY,
        result=EvidenceResult.PARTIAL, independent=True, task_id="t1",
    )
    repo.append_evidence(evidence)
    LearnerProfileService(repo, _ProfileRouter()).update_after_submission(
        project_id="p1", task=task,
        interaction=InteractionContext(
            activity_mode=ActivityMode.REVIEW, intervention_policy=InterventionPolicy.PROACTIVE,
            ui_preset=UIPreset.DEEP_LEARNING,
        ),
        judgment=AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.PARTIAL),
        answer_text="从队尾出队", evidence=evidence,
    )
    payload = learning_profile("p1", user=User(user_id="u1"), repo=repo)
    row = payload["records"][0]
    assert row["engine_state"]["current_verified_level"] == "L0"
    assert row["profile"]["next_practice_goal"]
    assert row["evidence_counts"]["independent_answers"] == 1
