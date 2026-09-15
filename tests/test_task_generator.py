"""Phase 5 tests: task generation — ARCHITECTURE §3.5."""

from __future__ import annotations

from types import SimpleNamespace

from bookmind.agents.bug_library import BUG_REF_VS_OBJECT
from bookmind.domain.enums import Level
from bookmind.engine.task.generator import generate_changed_task, generate_probe, generate_quiz
from bookmind.engine.task.validator import validate
from bookmind.storage.in_memory import InMemoryRepository
from bookmind.services.task_service import TaskService


def test_generate_probe_carries_discriminated_bug():
    draft = generate_probe(BUG_REF_VS_OBJECT, target_concept_ids=["c_reference"])
    assert draft.is_probe is True
    assert draft.discriminated_bug_ids == ["bug_ref_vs_object"]
    assert draft.prompt_text  # non-empty from template
    assert Level.L2 in draft.evidence_for_levels


def test_generate_changed_tasks_have_distinct_fingerprints():
    near = generate_changed_task(BUG_REF_VS_OBJECT, stage=1, target_concept_ids=["c_reference"])
    far = generate_changed_task(BUG_REF_VS_OBJECT, stage=2, target_concept_ids=["c_reference"])
    assert near.remediation_stage == 1
    assert far.remediation_stage == 2
    assert near.scenario_fingerprint != far.scenario_fingerprint
    assert near.is_changed_task is True
    assert far.is_changed_task is True


def test_generated_probe_passes_validator():
    repo = InMemoryRepository()
    draft = generate_probe(BUG_REF_VS_OBJECT, target_concept_ids=["c_reference"])
    report = validate(draft, repo, "p1")
    assert report.passed, report.blocked_reasons


def test_generated_changed_tasks_pass_validator():
    repo = InMemoryRepository()
    for stage in (1, 2):
        draft = generate_changed_task(BUG_REF_VS_OBJECT, stage=stage, target_concept_ids=["c_reference"])
        report = validate(draft, repo, "p1")
        assert report.passed, f"stage {stage}: {report.blocked_reasons}"


def test_generate_quiz_minimal():
    from bookmind.domain.models import Concept
    from bookmind.domain.enums import Difficulty
    c = Concept(concept_id="c_reference", book_id="b", name="References", importance=0.9,
                difficulty=Difficulty.MEDIUM)
    draft = generate_quiz(concept=c, level=Level.L1)
    # Each issued task gets a unique instance suffix (P0-02); the stable
    # template id is the prefix before the last "|<suffix>".
    assert draft.task_id.startswith("quiz|c_reference|")
    assert draft.task_id != "quiz|c_reference"
    assert draft.evidence_for_levels == [Level.L1]
    assert draft.rubric
    assert not draft.prompt_text.startswith("请解释")
    assert len(draft.rubric) >= 2


def test_generate_quiz_uses_live_model_for_grounded_reasoning_question():
    from bookmind.domain.models import Concept
    from bookmind.domain.enums import Difficulty

    class QuizRouter:
        cfg = SimpleNamespace(live=True)

        def __init__(self):
            self.calls = []

        def complete(self, task, messages, **kwargs):
            self.calls.append((task, messages, kwargs))
            return SimpleNamespace(
                ok=True,
                content="{}",
                parsed_json={
                    "prompt_text": "给定一个含 n 个元素的顺序表，比较在表头和表尾插入元素时的数据移动次数，并说明二者时间复杂度不同的原因。",
                    "expected_answer": "表头插入需要移动已有元素，表尾插入通常不需要移动已有元素，因此两者成本不同。",
                    "rubric": ["说明表头插入的移动", "说明表尾插入的成本", "比较两者复杂度"],
                },
            )

    concept = Concept(
        concept_id="c_complexity",
        book_id="b",
        name="时间复杂度",
        description="用输入规模描述算法基本操作次数的增长趋势。",
        importance=0.9,
        difficulty=Difficulty.MEDIUM,
    )
    router = QuizRouter()

    draft = generate_quiz(
        concept=concept,
        level=Level.L2,
        router=router,
        source_context="顺序表在表头插入时需要依次后移已有元素。",
    )

    assert draft.prompt_text.startswith("给定一个含 n 个元素")
    assert "表头插入" in draft.expected_answer
    assert len(draft.rubric) == 3
    assert router.calls[0][0] == "grounded_quiz_generation"
    assert "顺序表在表头插入" in router.calls[0][1][1]["content"]
    assert "output_schema" in router.calls[0][2]


def test_demo_concept_uses_curated_application_question_without_waiting_for_model():
    from bookmind.domain.models import Concept
    from bookmind.domain.enums import Difficulty

    class FailIfCalledRouter:
        cfg = SimpleNamespace(live=True)

        def complete(self, *args, **kwargs):
            raise AssertionError("curated demo questions must be immediate")

    concept = Concept(
        concept_id="demo_tree_traversal",
        book_id="b",
        name="二叉树的层次与遍历",
        importance=0.9,
        difficulty=Difficulty.HARD,
    )

    draft = generate_quiz(concept=concept, level=Level.L2, router=FailIfCalledRouter())

    assert "先序序列" in draft.prompt_text
    assert "后序" in draft.expected_answer
    assert len(draft.rubric) == 3


def test_practice_level_starts_at_l1_then_advances_with_verified_state():
    """The product task service must follow the same ladder as mastery state."""
    repo = InMemoryRepository()
    router = SimpleNamespace(cfg=SimpleNamespace(live=False))
    service = TaskService(repo, router, diagnostician=SimpleNamespace())

    assert service._next_quiz_level("p1", "c1") == Level.L1
    state = repo.get_state("p1", "c1")
    state.current_verified_level = Level.L1
    repo.save_state(state)
    assert service._next_quiz_level("p1", "c1") == Level.L2


def test_explanation_revealed_before_answer_closes_task_without_raw_retrieval():
    repo = InMemoryRepository()
    router = SimpleNamespace(cfg=SimpleNamespace(live=False))
    service = TaskService(repo, router, diagnostician=SimpleNamespace())
    repo.save_trusted_task({
        "task_id": "t1", "project_id": "p1", "conversation_id": "c1",
        "status": "PENDING", "prompt_text": "比较线性查找和二分查找的时间复杂度。",
        "expected_answer": "线性查找为 O(n)，有序数组上的二分查找为 O(log n)。",
        "rubric": ["给出 O(n)", "给出 O(log n)", "说明二分要求有序"],
        "target_concept_ids": [], "hints_issued": 0,
    })

    result = service.explain_task("t1")

    assert result["revealed_while_pending"] is True
    assert repo.get_trusted_task("t1")["status"] == "EXPLAINED"
    assert "参考结论" in result["text"]
    assert "O(log n)" in result["text"]
