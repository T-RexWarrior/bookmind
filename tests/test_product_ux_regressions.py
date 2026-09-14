"""User-facing regression tests for the post-productization cleanup."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from bookmind.api.app import create_app
from bookmind.domain.enums import EvidenceResult, JudgmentStatus
from bookmind.domain.models import AnswerJudgment, CriterionResult
from bookmind.services.conversation_orchestrator import classify_intent
from bookmind.services.task_service import _judgment_view
from bookmind.storage.sql import SqlRepository


FRONTEND_SRC = Path(__file__).parents[1] / "frontend" / "src"


def _client(tmp_path):
    repo = SqlRepository(f"sqlite:///{tmp_path / 'ux.db'}")
    repo.create_schema()
    return TestClient(create_app(repo=repo))


def _workspace(client: TestClient) -> tuple[str, str]:
    assert client.post("/api/session/bootstrap").status_code == 200
    project_id = client.post("/api/projects", json={"name": "体验测试"}).json()["project_id"]
    assert client.post(f"/api/projects/{project_id}/books/seed-demo").status_code == 200
    conversation_id = client.post(
        f"/api/projects/{project_id}/conversations",
        params={"activity_type": "ASSESSMENT"},
    ).json()["conversation_id"]
    return project_id, conversation_id


def _last_assistant_blocks(client: TestClient, conversation_id: str) -> list[dict]:
    messages = client.get(f"/api/conversations/{conversation_id}").json()["messages"]
    return [m for m in messages if m["role"] == "assistant"][-1]["content_blocks"]


def test_product_app_does_not_mount_legacy_write_api(tmp_path):
    client = _client(tmp_path)
    # The production app has one authoritative API surface. The old endpoint
    # must not be able to write to a hidden module-level in-memory repository.
    response = client.post("/users", json={"user_id": "legacy", "display_name": "legacy"})
    assert response.status_code == 404


def test_product_ui_exposes_one_consolidation_module_without_assessment_tab():
    switcher = (FRONTEND_SRC / "features" / "assessment" / "ModeSwitcher.tsx").read_text(
        encoding="utf-8"
    )
    overview = (
        FRONTEND_SRC / "features" / "assessment" / "ConsolidationOverview.tsx"
    ).read_text(encoding="utf-8")
    learning_sidebar = (
        FRONTEND_SRC / "features" / "learning" / "LearningSidebar.tsx"
    ).read_text(encoding="utf-8")

    assert switcher.count("练习巩固") >= 2
    assert "独立检测" not in switcher
    assert "consolidation-tabs" not in switcher
    assert "独立检测" not in overview
    assert "独立检测" not in learning_sidebar


def test_question_while_task_pending_is_not_graded():
    assert classify_intent("优先级队列是什么？", has_pending_task=True) == "REQUEST_EXPLANATION"
    assert classify_intent("?", has_pending_task=True) == "REQUEST_EXPLANATION"


def test_natural_task_requests_start_assessment_instead_of_book_qa():
    assert classify_intent("给我一道综合题。") == "REQUEST_TASK"
    assert classify_intent("来一道检测题") == "REQUEST_TASK"


def test_invalid_answer_keeps_original_task_and_hides_internal_reason(tmp_path):
    client = _client(tmp_path)
    _, conversation_id = _workspace(client)
    client.post(f"/api/conversations/{conversation_id}/messages", json={"content": "开始检测"})
    task = next(
        block["data"] for block in _last_assistant_blocks(client, conversation_id)
        if block["type"] == "task"
    )

    response = client.post(
        f"/api/tasks/{task['task_id']}/answer",
        json={"answer_text": "?", "idempotency_key": "invalid-1"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["needs_review"] is True
    assert body["written"] is False
    assert body["next_action"] is None
    assert "clarification" in body
    assert client.get(f"/api/tasks/{task['task_id']}").json()["status"] == "PENDING"


def test_repeated_start_request_does_not_duplicate_pending_task(tmp_path):
    client = _client(tmp_path)
    _, conversation_id = _workspace(client)
    client.post(f"/api/conversations/{conversation_id}/messages", json={"content": "开始检测"})
    first_blocks = _last_assistant_blocks(client, conversation_id)
    first_task = next(block["data"] for block in first_blocks if block["type"] == "task")

    client.post(f"/api/conversations/{conversation_id}/messages", json={"content": "再出几道题"})
    second_blocks = _last_assistant_blocks(client, conversation_id)
    assert not any(block["type"] == "task" for block in second_blocks)
    text = " ".join(block.get("text", "") for block in second_blocks)
    assert "上一道检测题" in text
    assert client.get(f"/api/tasks/{first_task['task_id']}").json()["status"] == "PENDING"


def test_learning_modes_keep_separate_conversation_histories(tmp_path):
    client = _client(tmp_path)
    project_id, assessment_id = _workspace(client)
    ids = {"ASSESSMENT": assessment_id}
    for activity in ("LEARN", "REVIEW"):
        response = client.post(
            f"/api/projects/{project_id}/conversations",
            params={"activity_type": activity},
        )
        ids[activity] = response.json()["conversation_id"]

    messages = {
        "LEARN": "请解释 equals",
        "REVIEW": "开始复习",
        "ASSESSMENT": "开始检测",
    }
    for activity, conversation_id in ids.items():
        response = client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": messages[activity]},
        )
        assert response.status_code == 200

    for activity, conversation_id in ids.items():
        listed = client.get(
            f"/api/projects/{project_id}/conversations",
            params={"activity_type": activity},
        ).json()
        assert {item["conversation_id"] for item in listed} == {conversation_id}
        history = client.get(f"/api/conversations/{conversation_id}").json()
        user_text = " ".join(
            block.get("text", "")
            for message in history["messages"] if message["role"] == "user"
            for block in message["content_blocks"]
        )
        assert messages[activity] in user_text
        assert all(other not in user_text for key, other in messages.items() if key != activity)


def test_public_judgment_hides_model_reason_and_rubric_details():
    judgment = AnswerJudgment(
        judgment_status=JudgmentStatus.DECIDED,
        result=EvidenceResult.FAIL,
        reason="offline: matched likely wrong answer",
        criterion_results=[
            CriterionResult(
                criterion_id="internal_rubric_reference_identity",
                satisfied=False,
                note="expected exact implementation detail",
            ),
        ],
    )
    public = _judgment_view(judgment)
    assert public["reason"] == "这次回答还没有体现出本题考查的关键理解。"
    assert public["criterion_results"] == [
        {"criterion_id": "1", "satisfied": False, "note": ""},
    ]
