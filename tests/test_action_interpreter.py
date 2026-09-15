"""Natural-language task action routing remains safe with and without a model."""

from types import SimpleNamespace

from bookmind.llm.schemas import ModelResult
from bookmind.services.conversation_orchestrator import classify_intent, interpret_intent
from bookmind.services.task_service import _clarification_for


def test_pending_task_fallback_has_distinct_safe_exits():
    assert classify_intent("我不知道", has_pending_task=True) == "UNSURE_OR_GIVE_UP"
    assert classify_intent("跳过这题", has_pending_task=True) == "SKIP_TASK"
    assert classify_intent("给点方向", has_pending_task=True) == "REQUEST_HINT"


def test_live_interpreter_accepts_only_confident_structured_action():
    router = SimpleNamespace(
        cfg=SimpleNamespace(live=True),
        complete=lambda *args, **kwargs: ModelResult(
            ok=True, task="action_interpretation", model="test",
            parsed_json={"intent": "SKIP_TASK", "confidence": 0.98},
        ),
    )
    assert interpret_intent(router, "这题先放着", has_pending_task=True, task_prompt="题干") == "SKIP_TASK"


def test_short_but_valid_answers_reach_the_judge():
    assert _clarification_for("O(1)") == ""
    assert _clarification_for("是") == ""
    assert _clarification_for("")
