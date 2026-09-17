"""M2 product API integration tests — the vertical closed loop over HTTP
(PRODUCTIZATION §13 M2 acceptance path).

  bootstrap → create project → seed-demo → conversation → ask → SSE events →
  grounded answer with citations → reload → messages persist.

No internal IDs are typed by the "user"; the learner is derived from the
session cookie. Runs against a SqlRepository so persistence is real.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bookmind.api.app import create_app
from bookmind.storage.sql import SqlRepository


@pytest.fixture()
def client():
    # Use a SqlRepository so the conversation layer persists (the /api/* path).
    repo = SqlRepository("sqlite:///:memory:")
    repo.create_schema()
    app = create_app(repo=repo)
    return TestClient(app)


def _bootstrap(client):
    r = client.post("/api/session/bootstrap")
    assert r.status_code == 200
    return r.json()


def test_bootstrap_sets_cookie_and_me(client):
    me_body = _bootstrap(client)
    assert "user_id" in me_body
    # The cookie is set automatically on the TestClient.
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json()["user_id"] == me_body["user_id"]
    # No internal ID is ever exposed in a way the user must type.


def test_langgraph_workflow_topology_is_live(client):
    response = client.get("/api/agent-workflow")
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"] == "langgraph"
    assert body["workflow"] == "bookmind_learning_conversation"
    for node in (
        "classify_intent", "book_qa", "start_learning", "generate_task",
        "diagnose_answer", "switch_mode", "show_progress",
    ):
        assert node in body["mermaid"]


def test_unsigned_user_id_cookie_is_rejected(client):
    me_body = _bootstrap(client)
    client.cookies.set("bookmind_session", me_body["user_id"])
    assert client.get("/api/me").status_code == 401


def test_full_qa_vertical_loop(client):
    """The M2 acceptance path: ask a book question, get a grounded cited
    answer, reload, and the conversation + answer persist."""
    _bootstrap(client)

    # Create a project (server-generated id).
    r = client.post("/api/projects", json={"name": "Java OOP", "goal": "学完这本书"})
    assert r.status_code == 200
    pid = r.json()["project_id"]

    # Seed the demo textbook.
    r = client.post(f"/api/projects/{pid}/books/seed-demo")
    assert r.status_code == 200
    assert r.json()["concepts"] > 0 and r.json()["chunks"] > 0

    # Create a conversation.
    r = client.post(f"/api/projects/{pid}/conversations")
    assert r.status_code == 200
    conv_id = r.json()["conversation_id"]

    # Send a book question.
    r = client.post(f"/api/conversations/{conv_id}/messages",
                    json={"content": "== 和 equals 的区别"})
    assert r.status_code == 200
    body = r.json()
    run_id = body["run_id"]
    assert body["status"] == "COMPLETED"

    # Replay the run's SSE events.
    r = client.get(f"/api/runs/{run_id}/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    # The stream should contain run_started, agent_delta, run_completed at least.
    assert "run_started" in r.text
    assert "run_completed" in r.text
    assert '"workflow": "langgraph"' in r.text

    # The conversation now has a user + an assistant message.
    r = client.get(f"/api/conversations/{conv_id}")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    # The assistant message has structured content blocks.
    blocks = msgs[1]["content_blocks"]
    assert any(b["type"] == "text" for b in blocks)


def test_grounded_question_records_doubt_without_changing_mastery(client):
    """Asking is a QUESTION signal, never proof of knowing or not knowing."""
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "疑问记录"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    cid = client.post(f"/api/projects/{pid}/conversations").json()["conversation_id"]

    sent = client.post(
        f"/api/conversations/{cid}/messages",
        json={"content": "== 和 equals 有什么区别？"},
    )
    assert sent.status_code == 200

    summary = client.get(f"/api/projects/{pid}/learning-summary").json()
    questioned = [item for item in summary["concepts"] if item["question_count"] > 0]
    assert summary["questioned_count"] > 0
    assert questioned
    assert all(item["level"] == "L0" for item in questioned)
    assert all(item["group"] == "pending" for item in questioned)

    blocks = client.get(f"/api/conversations/{cid}").json()["messages"][-1]["content_blocks"]
    signal = next(block["data"] for block in blocks if block["type"] == "question_signal")
    signal_copy = signal["message"]
    assert "不会改变掌握状态" in signal_copy
    assert {item["concept_id"] for item in signal["concepts"]}

    # The combined consolidation module can explicitly turn that doubt signal
    # into a task candidate without treating the question itself as failure.
    client.patch(f"/api/projects/{pid}", json={"default_mode": "Review"})
    practice_cid = client.post(
        f"/api/projects/{pid}/conversations",
        params={"activity_type": "REVIEW"},
    ).json()["conversation_id"]
    practice = client.post(
        f"/api/conversations/{practice_cid}/messages",
        json={"content": "从我问过的知识点出一道题"},
    )
    assert practice.status_code == 200
    practice_blocks = client.get(
        f"/api/conversations/{practice_cid}",
    ).json()["messages"][-1]["content_blocks"]
    task = next(block["data"] for block in practice_blocks if block["type"] == "task")
    assert task["focus"] in {item["name"] for item in questioned}


def test_explicit_consolidation_buttons_use_structured_task_api(client):
    """The product buttons use explicit candidates/tasks APIs, not chat text."""
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "显式巩固"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    learn_cid = client.post(f"/api/projects/{pid}/conversations").json()["conversation_id"]
    client.post(
        f"/api/conversations/{learn_cid}/messages",
        json={"content": "== 和 equals 有什么区别？"},
    )

    candidates = client.get(
        f"/api/projects/{pid}/consolidation-candidates",
        params={"mode": "PRACTICE", "filter": "QUESTIONED"},
    )
    assert candidates.status_code == 200
    queue = candidates.json()
    assert queue["counts"]["questioned"] > 0
    assert queue["candidates"]
    selected = queue["candidates"][0]
    assert selected["reason_code"] == "QUESTIONED"

    cid = client.post(
        f"/api/projects/{pid}/conversations",
        params={"activity_type": "REVIEW"},
    ).json()["conversation_id"]
    created = client.post(
        f"/api/conversations/{cid}/tasks",
        json={
            "mode": "PRACTICE",
            "selection": "QUESTIONED",
            "concept_id": selected["concept_id"],
            "idempotency_key": "button-1",
        },
    )
    assert created.status_code == 200
    assert created.json()["task"]["focus"] == selected["name"]
    assert created.json()["existing"] is False
    serialized = str(created.json()["task"])
    for secret in ("target_concept_ids", "selected_concept_id", "rubric", "expected_answer"):
        assert secret not in serialized

    # A button action adds only an assistant task card; it never fabricates a
    # user message such as “开始检测” in the conversation history.
    messages = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["content_blocks"][0]["type"] == "task"

    replay = client.post(
        f"/api/conversations/{cid}/tasks",
        json={"mode": "PRACTICE", "selection": "RECOMMENDED"},
    )
    assert replay.status_code == 200
    assert replay.json()["existing"] is True
    assert len(client.get(f"/api/conversations/{cid}").json()["messages"]) == 1


def test_explicit_task_api_rejects_activity_and_scope_mismatch(client):
    me = _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "边界"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    learn_cid = client.post(f"/api/projects/{pid}/conversations").json()["conversation_id"]

    wrong_activity = client.post(
        f"/api/conversations/{learn_cid}/tasks",
        json={"mode": "ASSESSMENT"},
    )
    assert wrong_activity.status_code == 409
    assert wrong_activity.json()["error"]["code"] == "ACTIVITY_MISMATCH"

    assessment_cid = client.post(
        f"/api/projects/{pid}/conversations",
        params={"activity_type": "ASSESSMENT"},
    ).json()["conversation_id"]
    wrong_concept = client.post(
        f"/api/conversations/{assessment_cid}/tasks",
        json={"mode": "ASSESSMENT", "concept_id": "concept_from_another_project"},
    )
    assert wrong_concept.status_code == 404
    assert wrong_concept.json()["error"]["code"] == "CONCEPT_NOT_IN_SCOPE"


def test_conversation_persists_across_reload(client):
    """Reloading the conversation (a fresh GET) still returns the messages —
    the M2 'refresh the page and the answer is still there' criterion."""
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "P"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    conv_id = client.post(f"/api/projects/{pid}/conversations").json()["conversation_id"]
    client.post(f"/api/conversations/{conv_id}/messages",
                json={"content": "什么是多态"})

    # Simulate a page reload: ask the conversation again from scratch.
    r = client.get(f"/api/conversations/{conv_id}")
    msgs = r.json()["messages"]
    assert len(msgs) == 2
    assert msgs[0]["content_blocks"][0]["text"] == "什么是多态"


def test_cross_user_isolation(client):
    """User A cannot read user B's project or conversation (PRODUCTIZATION §11.2)."""
    _bootstrap(client)
    pid_a = client.post("/api/projects", json={"name": "A"}).json()["project_id"]

    # A second bootstrap on a *new* client (different cookie).
    client2 = TestClient(create_app(repo=client.app.state.repo))
    _bootstrap(client2)

    # User B lists projects — must not see A's project.
    r = client2.get("/api/projects")
    assert pid_a not in [p["project_id"] for p in r.json()]

    # User B cannot fetch A's project directly.
    r = client2.get(f"/api/projects/{pid_a}")
    assert r.status_code in (403, 404, 500)


def test_learning_summary_after_seed(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "P"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    r = client.get(f"/api/projects/{pid}/learning-summary")
    assert r.status_code == 200
    body = r.json()
    assert body["total_concepts"] > 0
    # All concepts start in the "pending" group (nothing verified yet).
    assert "pending" in body["groups"]


def test_manual_learned_is_persistent_but_never_grants_mastery(client):
    """A learner declaration is visible in the shared profile, not evidence."""
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "自报学习状态"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    before = client.get(f"/api/projects/{pid}/learning-summary").json()
    concept_id = before["concepts"][0]["concept_id"]

    marked = client.put(
        f"/api/projects/{pid}/concepts/{concept_id}/manual-learning",
        json={"learned": True},
    )
    assert marked.status_code == 200
    assert marked.json()["label"] == "已学（待验证）"

    after = client.get(f"/api/projects/{pid}/learning-summary").json()
    row = next(item for item in after["concepts"] if item["concept_id"] == concept_id)
    assert row["manual_learned"] is True
    assert row["level"] == "L0"
    memory = client.get(f"/api/projects/{pid}/memory").json()
    assert any(item["kind"] == "MANUAL_LEARNED" and item["concept_id"] == concept_id for item in memory)

    cleared = client.put(
        f"/api/projects/{pid}/concepts/{concept_id}/manual-learning",
        json={"learned": False},
    )
    assert cleared.status_code == 200
    refreshed = client.get(f"/api/projects/{pid}/learning-summary").json()
    assert next(item for item in refreshed["concepts"] if item["concept_id"] == concept_id)["manual_learned"] is False


def test_random_practice_and_post_task_followup_are_state_safe(client):
    """The primary practice action is random; its follow-up is read-only."""
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "随机练习"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    cid = client.post(
        f"/api/projects/{pid}/conversations", params={"activity_type": "REVIEW"},
    ).json()["conversation_id"]

    created = client.post(
        f"/api/conversations/{cid}/tasks",
        json={"mode": "PRACTICE", "selection": "RANDOM"},
    )
    assert created.status_code == 200
    task_id = created.json()["task"]["task_id"]
    assert client.post(f"/api/tasks/{task_id}/skip").status_code == 200

    before = client.get(f"/api/projects/{pid}/learning-summary").json()
    followup = client.post(
        f"/api/conversations/{cid}/tasks/{task_id}/followup",
        json={"question": "为什么这个结论成立？"},
    )
    assert followup.status_code == 200
    assert followup.json()["read_only"] is True
    # The follow-up phase is persistent and blocks every new-task entrance,
    # including a direct API call that would bypass disabled browser buttons.
    state = client.get(f"/api/conversations/{cid}").json()["practice_state"]
    assert state == {"phase": "FOLLOWUP", "task_id": task_id}
    blocked = client.post(
        f"/api/conversations/{cid}/tasks",
        json={"mode": "PRACTICE", "selection": "RANDOM"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "FOLLOWUP_ACTIVE"
    assert client.post(f"/api/conversations/{cid}/consolidation-summary").status_code == 409
    closed = client.post(f"/api/conversations/{cid}/tasks/{task_id}/followup/close")
    assert closed.status_code == 200
    assert client.get(f"/api/conversations/{cid}").json()["practice_state"]["phase"] == "IDLE"
    after = client.get(f"/api/projects/{pid}/learning-summary").json()
    assert [(item["concept_id"], item["level"]) for item in before["concepts"]] == [
        (item["concept_id"], item["level"]) for item in after["concepts"]
    ]
    memory = client.get(f"/api/projects/{pid}/memory").json()
    assert any(item["kind"] == "TASK_FOLLOWUP" for item in memory)


def test_every_terminal_task_path_exposes_the_same_followup_exit(client):
    """Skip and explanation must not strand the learner outside follow-up.

    The browser renders one `task_complete` card for every terminal path; this
    API test protects the server contract that makes that state machine real.
    """
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "统一结束出口"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    cid = client.post(
        f"/api/projects/{pid}/conversations", params={"activity_type": "REVIEW"},
    ).json()["conversation_id"]
    created = client.post(
        f"/api/conversations/{cid}/tasks",
        json={"mode": "PRACTICE", "selection": "RANDOM"},
    )
    assert created.status_code == 200
    task_id = created.json()["task"]["task_id"]

    skipped = client.post(f"/api/tasks/{task_id}/skip")
    assert skipped.status_code == 200
    skip_blocks = client.get(f"/api/conversations/{cid}").json()["messages"][-1]["content_blocks"]
    assert any(
        block.get("data", {}).get("kind") == "task_complete"
        and block["data"].get("completion_status") == "SKIPPED"
        for block in skip_blocks
    )

    explained = client.post(f"/api/conversations/{cid}/tasks/{task_id}/explanation")
    assert explained.status_code == 200
    explanation_blocks = client.get(f"/api/conversations/{cid}").json()["messages"][-1]["content_blocks"]
    assert any(
        block.get("data", {}).get("kind") == "task_complete"
        and block["data"].get("completion_status") == "EXPLAINED"
        for block in explanation_blocks
    )

    # A terminal task can open a read-only follow-up, and that phase is the
    # only state that blocks creating the next task.
    started = client.post(f"/api/conversations/{cid}/tasks/{task_id}/followup/start")
    assert started.status_code == 200
    start_trace = client.get(f"/api/runs/{started.json()['run_id']}/trace").json()
    assert start_trace["run"]["intent"] == "START_TASK_FOLLOWUP"
    assert client.post(
        f"/api/conversations/{cid}/tasks", json={"mode": "PRACTICE", "selection": "RANDOM"},
    ).status_code == 409
    closed = client.post(f"/api/conversations/{cid}/tasks/{task_id}/followup/close")
    assert closed.status_code == 200
    close_trace = client.get(f"/api/runs/{closed.json()['run_id']}/trace").json()
    assert close_trace["run"]["intent"] == "END_TASK_FOLLOWUP"


def test_mode_command_and_start_learning_are_real_handlers(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "P"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    cid = client.post(f"/api/projects/{pid}/conversations").json()["conversation_id"]

    response = client.post(
        f"/api/conversations/{cid}/messages",
        json={"content": "帮我从第一章开始学习"},
    )
    assert response.status_code == 200
    messages = client.get(f"/api/conversations/{cid}").json()["messages"]
    first_reply = messages[-1]["content_blocks"]
    assert any("我们从" in (block.get("text") or "") for block in first_reply)

    response = client.post(
        f"/api/conversations/{cid}/messages",
        json={"content": "切换模式到评估模式"},
    )
    assert response.status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["default_mode"] == "Assessment"


def test_knowledge_graph_endpoint_returns_nodes_edges_and_sources(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "P"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    body = client.get(f"/api/projects/{pid}/knowledge-graph").json()
    assert body["stats"]["concepts"] == len(body["nodes"])
    assert body["stats"]["relations"] == len(body["edges"])
    assert body["nodes"] and body["edges"]
    assert body["source_ids"] == body["book_ids"]


def test_learning_space_metadata_and_resume_position(client):
    _bootstrap(client)
    created = client.post("/api/projects", json={
        "name": "论文阅读",
        "goal": "理解方法并复现实验",
        "learning_scope": "方法与实验章节",
        "deadline": "2027-01-15",
        "current_plan": "先读方法，再整理实验变量",
    })
    assert created.status_code == 200
    pid = created.json()["project_id"]

    patched = client.patch(f"/api/projects/{pid}", json={
        "last_source_id": "source_resume",
        "last_source_page": 18,
        "current_plan": "继续阅读第 18 页",
    })
    assert patched.status_code == 200
    project = client.get(f"/api/projects/{pid}").json()
    assert project["goal"] == "理解方法并复现实验"
    assert project["learning_scope"] == "方法与实验章节"
    assert project["deadline"] == "2027-01-15"
    assert project["current_plan"] == "继续阅读第 18 页"
    assert project["last_source_id"] == "source_resume"
    assert project["last_source_page"] == 18


def test_generic_source_api_and_answer_provenance(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "通用资料空间"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")

    sources = client.get(f"/api/projects/{pid}/sources")
    assert sources.status_code == 200
    source = sources.json()[0]
    assert source["source_id"] == source["book_id"]
    assert "source_type" in source and "outline" in source

    cid = client.post(f"/api/projects/{pid}/conversations").json()["conversation_id"]
    sent = client.post(f"/api/conversations/{cid}/messages", json={
        "content": "什么是多态？",
        "source_id": source["source_id"],
        "source_scope": "CURRENT_SOURCE",
    })
    assert sent.status_code == 200
    blocks = client.get(f"/api/conversations/{cid}").json()["messages"][-1]["content_blocks"]
    context = next(block for block in blocks if block["type"] == "context")
    assert context["data"]["scope"] == "当前资料"
    assert context["data"]["reason"]
    assert context["data"]["items"]
    assert all(item["source_id"] == source["source_id"] for item in context["data"]["items"])


def test_selected_pdf_text_is_scoped_persisted_and_used_as_context(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "划词提问"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    source = client.get(f"/api/projects/{pid}/sources").json()[0]
    cid = client.post(f"/api/projects/{pid}/conversations").json()["conversation_id"]

    sent = client.post(f"/api/conversations/{cid}/messages", json={
        "content": "请解释这段原文。",
        "source_id": source["source_id"],
        "source_page": 89,
        "source_scope": "CURRENT_PAGE",
        "selection_text": "多态允许同一个接口呈现不同的实现行为。",
    })
    assert sent.status_code == 200
    messages = client.get(f"/api/conversations/{cid}").json()["messages"]
    selected = next(
        block["data"] for block in messages[0]["content_blocks"]
        if block["type"] == "context"
    )
    assert selected["kind"] == "selection_context"
    assert selected["page"] == 89
    assert "多态" in selected["quote"]
    answer_context = next(
        block["data"] for block in messages[1]["content_blocks"]
        if block["type"] == "context"
    )
    assert "选中的原文" in answer_context["reason"]

    rejected = client.post(f"/api/conversations/{cid}/messages", json={
        "content": "解释",
        "source_id": "source_not_owned",
        "source_page": 1,
        "source_scope": "CURRENT_PAGE",
        "selection_text": "越权内容",
    })
    assert rejected.status_code == 404
    assert rejected.json()["error"]["code"] == "SOURCE_NOT_IN_SCOPE"


def test_learning_record_and_post_task_buttons_have_explicit_apis(client):
    _bootstrap(client)
    pid = client.post("/api/projects", json={"name": "完整交互"}).json()["project_id"]
    client.post(f"/api/projects/{pid}/books/seed-demo")
    learn_cid = client.post(f"/api/projects/{pid}/conversations").json()["conversation_id"]
    client.post(
        f"/api/conversations/{learn_cid}/messages",
        json={"content": "== 和 equals 有什么区别？"},
    )
    summary = client.get(f"/api/projects/{pid}/learning-summary").json()
    questioned = next(item for item in summary["concepts"] if item["question_count"] > 0)

    record_response = client.get(
        f"/api/projects/{pid}/concepts/{questioned['concept_id']}/record",
    )
    assert record_response.status_code == 200
    record = record_response.json()
    assert record["question_count"] > 0
    assert record["timeline"][0]["type"] == "QUESTION"
    assert record["source_refs"]

    cid = client.post(
        f"/api/projects/{pid}/conversations",
        params={"activity_type": "REVIEW"},
    ).json()["conversation_id"]
    first = client.post(f"/api/conversations/{cid}/tasks", json={
        "mode": "PRACTICE", "concept_id": questioned["concept_id"],
    }).json()["task"]
    client.post(f"/api/tasks/{first['task_id']}/skip")

    explanation = client.post(
        f"/api/conversations/{cid}/tasks/{first['task_id']}/explanation",
    )
    assert explanation.status_code == 200
    messages = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert any(block["type"] == "text" for block in messages[-1]["content_blocks"])

    targeted = client.post(f"/api/conversations/{cid}/tasks", json={
        "mode": "PRACTICE", "from_task_id": first["task_id"],
    })
    assert targeted.status_code == 200
    assert targeted.json()["task"]["focus"] == first["focus"]
    client.post(f"/api/tasks/{targeted.json()['task']['task_id']}/skip")

    finished = client.post(f"/api/conversations/{cid}/consolidation-summary")
    assert finished.status_code == 200
    assert finished.json()["total"] == 0
    final_message = client.get(f"/api/conversations/{cid}").json()["messages"][-1]
    assert final_message["content_blocks"][0]["data"]["kind"] == "consolidation_summary"
