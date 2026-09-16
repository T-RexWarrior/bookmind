"""Regression coverage for the merged textbook-QA and learning-task branches."""

import time

from fastapi.testclient import TestClient

from bookmind.api.app import create_app
from bookmind.storage.sql import SqlRepository


def test_structured_task_lifecycle_preserves_learning_state_safety_without_leaks():
    repo = SqlRepository("sqlite:///:memory:")
    repo.create_schema()

    with TestClient(create_app(repo=repo)) as client:
        assert client.post("/api/session/bootstrap").status_code == 200
        project_id = client.post("/api/projects", json={"name": "合并验证"}).json()["project_id"]
        assert client.post(f"/api/projects/{project_id}/books/seed-demo").status_code == 200

        conversation_id = client.post(
            f"/api/projects/{project_id}/conversations",
            params={"activity_type": "REVIEW"},
        ).json()["conversation_id"]

        created = client.post(
            f"/api/conversations/{conversation_id}/tasks",
            json={
                "mode": "PRACTICE",
                "selection": "ALL",
                "idempotency_key": "merge-1",
            },
        )
        assert created.status_code == 200, created.text
        body = created.json()
        task = body["task"]
        assert body["message"]["content_blocks"][0]["data"]["task_id"] == task["task_id"]
        assert not ({"rubric", "expected_answer", "target_concept_ids"} & set(task))

        hint = client.post(f"/api/tasks/{task['task_id']}/hint")
        assert hint.status_code == 200
        assert hint.json()["hints_issued"] == 1
        assert client.post(f"/api/tasks/{task['task_id']}/skip").json()["status"] == "SKIPPED"

        next_created = client.post(
            f"/api/conversations/{conversation_id}/tasks",
            json={
                "mode": "PRACTICE",
                "selection": "ALL",
                "from_task_id": task["task_id"],
                "idempotency_key": "merge-2",
            },
        )
        assert next_created.status_code == 200, next_created.text
        next_task_id = next_created.json()["task"]["task_id"]
        assert next_task_id != task["task_id"]
        trusted = repo.get_trusted_task(next_task_id)
        assert trusted is not None

        judged = client.post(
            f"/api/tasks/{next_task_id}/answer",
            json={"answer_text": trusted["expected_answer"], "idempotency_key": "merge-answer-1"},
        )
        assert judged.status_code == 200, judged.text
        judgment_body = judged.json()
        result = judgment_body["judgment"]["result"]
        if result is None:
            # With no live model, prose-only answers must remain ungraded
            # instead of fabricating mastery evidence.
            assert judgment_body["needs_review"] is True
            assert judgment_body["written"] is False
        else:
            assert result in {"PASS", "PARTIAL", "FAIL"}

        summary = client.get(f"/api/projects/{project_id}/learning-summary")
        assert summary.status_code == 200
        assert summary.json()["total_concepts"] > 0


def test_assessment_task_uses_separate_conversation_and_safe_card():
    repo = SqlRepository("sqlite:///:memory:")
    repo.create_schema()

    with TestClient(create_app(repo=repo)) as client:
        client.post("/api/session/bootstrap")
        project_id = client.post("/api/projects", json={"name": "评估验证"}).json()["project_id"]
        client.post(f"/api/projects/{project_id}/books/seed-demo")
        conversation_id = client.post(
            f"/api/projects/{project_id}/conversations",
            params={"activity_type": "ASSESSMENT"},
        ).json()["conversation_id"]

        response = client.post(
            f"/api/conversations/{conversation_id}/tasks",
            json={"mode": "ASSESSMENT", "selection": "ALL"},
        )
        assert response.status_code == 200, response.text
        task = response.json()["task"]
        assert task["task_id"]
        assert task["prompt_text"]
        assert not ({"rubric", "expected_answer", "target_concept_ids", "discriminated_bug_ids"} & set(task))


def test_async_chat_action_can_skip_a_structured_pending_task(tmp_path):
    # Use file-backed SQLite here: the chat worker runs on another thread and
    # an in-memory StaticPool deliberately shares one connection, which can
    # make independent test sessions race even though production SQLite does not.
    repo = SqlRepository(f"sqlite:///{tmp_path}/async-chat.db")
    repo.create_schema()

    with TestClient(create_app(repo=repo)) as client:
        client.post("/api/session/bootstrap")
        project_id = client.post("/api/projects", json={"name": "异步动作验证"}).json()["project_id"]
        client.post(f"/api/projects/{project_id}/books/seed-demo")
        conversation_id = client.post(
            f"/api/projects/{project_id}/conversations",
            params={"activity_type": "REVIEW"},
        ).json()["conversation_id"]
        task_id = client.post(
            f"/api/conversations/{conversation_id}/tasks",
            json={"mode": "PRACTICE", "selection": "ALL"},
        ).json()["task"]["task_id"]

        queued = client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "跳过这题", "idempotency_key": "merge-skip-chat"},
        )
        assert queued.status_code == 202

        deadline = time.monotonic() + 3
        status = "PENDING"
        while time.monotonic() < deadline:
            status = client.get(f"/api/tasks/{task_id}").json()["status"]
            if status == "SKIPPED":
                break
            time.sleep(0.02)
        assert status == "SKIPPED"

        messages = client.get(f"/api/conversations/{conversation_id}").json()["messages"]
        assert any(
            block.get("type") == "text" and "不会把它记为错误" in block.get("text", "")
            for message in messages if message["role"] == "assistant"
            for block in message["content_blocks"]
        )
