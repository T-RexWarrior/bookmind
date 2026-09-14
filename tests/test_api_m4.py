"""M4 product API integration tests — real task & diagnosis closed loop
(PRODUCTIZATION §M4 acceptance path).

Covers:
  request task → task card (no rubric/concept_id leaked) → answer submission
  (only task_id + answer_text) → judgment → Evidence written → idempotent
  resubmit → hint counts → NEEDS_REVIEW writes no Evidence → cross-user
  isolation → skip.

Runs against a SqlRepository so persistence is real, matching the M3 pattern.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bookmind.api.app import create_app
from bookmind.storage.sql import SqlRepository


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKMIND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOOKMIND_DATABASE_URL", f"sqlite:///{tmp_path}/bm.db")
    from bookmind.config import get_settings
    get_settings.cache_clear()
    repo = SqlRepository(f"sqlite:///{tmp_path}/bm.db")
    repo.create_schema()
    app = create_app(repo=repo)
    with TestClient(app) as c:
        yield c


def _bootstrap(client):
    r = client.post("/api/session/bootstrap")
    assert r.status_code == 200
    return r.json()


def _seed_demo(client, project_id):
    r = client.post(f"/api/projects/{project_id}/books/seed-demo")
    assert r.status_code == 200, r.text


def _make_project(client, name="Java 学习"):
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["project_id"]


def _request_task_via_orchestrator(client, project_id, text="考考我"):
    """Drive REQUEST_TASK through the conversation path (the real user flow)."""
    # Create a conversation.
    r = client.post(f"/api/projects/{project_id}/conversations", json={})
    assert r.status_code == 200, r.text
    cid = r.json()["conversation_id"]
    # Send the message — the orchestrator classifies REQUEST_TASK and emits a task block.
    r = client.post(f"/api/conversations/{cid}/messages", json={"content": text})
    assert r.status_code == 200, r.text
    body = r.json()
    run_id = body["run_id"]
    # Fetch the conversation to get the persisted assistant message blocks.
    r = client.get(f"/api/conversations/{cid}")
    assert r.status_code == 200, r.text
    messages = r.json()["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"][-1]
    return cid, run_id, assistant["content_blocks"]


def _find_task_block(blocks):
    for b in blocks:
        if b.get("type") == "task" and b.get("data", {}).get("kind") in ("probe", "changed_task", "quiz"):
            return b["data"]
    return None


# --- task generation -------------------------------------------------------

def test_request_task_generates_card_without_leaks(client):
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid, run_id, blocks = _request_task_via_orchestrator(client, pid)
    task = _find_task_block(blocks)
    assert task is not None, f"no task block in {blocks}"
    assert task["prompt_text"], "prompt must be non-empty"
    # The browser-facing card MUST NOT leak internal fields.
    for forbidden in ("rubric", "expected_answer", "target_concept_ids", "discriminated_bug_ids"):
        assert forbidden not in task, f"task card leaked {forbidden}"
    # The task is persisted as PENDING.
    r = client.get(f"/api/tasks/{task['task_id']}")
    assert r.status_code == 200
    view = r.json()
    assert view["status"] == "PENDING"
    assert view["prompt_text"] == task["prompt_text"]
    for forbidden in ("rubric", "expected_answer", "target_concept_ids", "discriminated_bug_ids"):
        assert forbidden not in view


# --- answer submission contract -------------------------------------------

def test_submit_answer_only_accepts_task_id_and_text(client):
    """The answer endpoint takes ONLY answer_text + idempotency_key. Passing
    result/rubric/concept_id must be ignored (not echoed, not used)."""
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid, run_id, blocks = _request_task_via_orchestrator(client, pid)
    task = _find_task_block(blocks)
    assert task is not None
    # Deliberately send extra forbidden fields; the server must ignore them.
    r = client.post(f"/api/tasks/{task['task_id']}/answer", json={
        "answer_text": "这是一段答案",
        "idempotency_key": "k-1",
        "result": "PASS",                 # must be ignored
        "rubric": ["fake"],               # must be ignored
        "target_concept_ids": ["c_fake"], # must be ignored
        "misconception_signals": [],      # must be ignored
    })
    assert r.status_code == 200, r.text
    res = r.json()
    # Offline (no LLM) the Diagnostician returns NEEDS_REVIEW → no Evidence.
    assert res["needs_review"] is True
    assert res["written"] is False
    assert res["evidence_id"] is None


def test_idempotent_resubmit_returns_replay(client):
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid, run_id, blocks = _request_task_via_orchestrator(client, pid)
    task = _find_task_block(blocks)
    assert task is not None
    r1 = client.post(f"/api/tasks/{task['task_id']}/answer",
                     json={"answer_text": "答案", "idempotency_key": "idem-A"})
    assert r1.status_code == 200
    r2 = client.post(f"/api/tasks/{task['task_id']}/answer",
                     json={"answer_text": "不同的答案", "idempotency_key": "idem-A"})
    assert r2.status_code == 200
    assert r2.json().get("replay") is True
    assert r2.json()["written"] is False


def test_invalid_submission_keeps_task_pending(client):
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid, run_id, blocks = _request_task_via_orchestrator(client, pid)
    task = _find_task_block(blocks)
    assert task is not None
    response = client.post(f"/api/tasks/{task['task_id']}/answer",
                           json={"answer_text": "x", "idempotency_key": "k1"})
    assert response.status_code == 200
    assert response.json()["needs_review"] is True
    assert response.json()["written"] is False
    view = client.get(f"/api/tasks/{task['task_id']}").json()
    assert view["status"] == "PENDING"


# --- hint counting --------------------------------------------------------

def test_hint_increments_count_and_returns_notice(client):
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid, run_id, blocks = _request_task_via_orchestrator(client, pid)
    task = _find_task_block(blocks)
    assert task is not None
    r = client.post(f"/api/tasks/{task['task_id']}/hint")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hints_issued"] == 1
    assert "不会作为独立掌握证据" in body["hint_notice"]
    assert body["hint_text"]
    # A second hint increments.
    r2 = client.post(f"/api/tasks/{task['task_id']}/hint")
    assert r2.json()["hints_issued"] == 2
    # The hint count is reflected on the task view.
    view = client.get(f"/api/tasks/{task['task_id']}").json()
    assert view["hints_issued"] == 2


# --- skip -----------------------------------------------------------------

def test_skip_task_clears_pending(client):
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid, run_id, blocks = _request_task_via_orchestrator(client, pid)
    task = _find_task_block(blocks)
    assert task is not None
    r = client.post(f"/api/tasks/{task['task_id']}/skip")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "SKIPPED"
    view = client.get(f"/api/tasks/{task['task_id']}").json()
    assert view["status"] == "SKIPPED"
    # Cannot request a hint on a skipped task.
    r2 = client.post(f"/api/tasks/{task['task_id']}/hint")
    assert r2.status_code == 409


# --- cross-user isolation -------------------------------------------------

def test_cross_user_cannot_access_others_task(client):
    # User A
    _bootstrap(client)
    pid_a = _make_project(client, "A 的项目")
    _seed_demo(client, pid_a)
    cid_a, run_a, blocks_a = _request_task_via_orchestrator(client, pid_a)
    task_a = _find_task_block(blocks_a)
    assert task_a is not None
    task_id = task_a["task_id"]
    # User B — a fresh TestClient with a new session cookie.
    from fastapi.testclient import TestClient
    client_b = TestClient(client.app)
    client_b.post("/api/session/bootstrap")
    # B must not read A's task.
    r = client_b.get(f"/api/tasks/{task_id}")
    assert r.status_code in (403, 404)
    # B must not answer A's task.
    r2 = client_b.post(f"/api/tasks/{task_id}/answer",
                       json={"answer_text": "x", "idempotency_key": "k"})
    assert r2.status_code in (403, 404)


# --- unknown task ---------------------------------------------------------

def test_unknown_task_returns_404(client):
    _bootstrap(client)
    r = client.get("/api/tasks/does-not-exist")
    assert r.status_code == 404
    r2 = client.post("/api/tasks/does-not-exist/answer",
                     json={"answer_text": "x", "idempotency_key": "k"})
    assert r2.status_code == 404


# --- unauthenticated ------------------------------------------------------

def test_tasks_require_session(client):
    # No bootstrap → no session cookie.
    r = client.get("/api/tasks/whatever")
    assert r.status_code == 401


# --- submit-via-chat path (SUBMIT_ANSWER intent) --------------------------

def test_chat_answer_routed_as_submit_when_task_pending(client):
    """When a pending task exists, typing an answer in the chat box routes to
    SUBMIT_ANSWER and produces a judgment card (not a book-Q&A answer)."""
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid, run_id, blocks = _request_task_via_orchestrator(client, pid)
    task = _find_task_block(blocks)
    assert task is not None
    # Now type an answer in the chat — orchestrator should classify SUBMIT_ANSWER.
    r = client.post(f"/api/conversations/{cid}/messages",
                    json={"content": "这是我在聊天框里输入的答案"})
    assert r.status_code == 200, r.text
    run_id2 = r.json()["run_id"]
    # The run's intent should be SUBMIT_ANSWER.
    r_run = client.get(f"/api/runs/{run_id2}/events", headers={"Accept": "text/event-stream"})
    assert "SUBMIT_ANSWER" in r_run.text or "submit_answer" in r_run.text.lower() or "action_selected" in r_run.text
    # Fetch the conversation: the latest assistant message should be a judgment card.
    conv = client.get(f"/api/conversations/{cid}").json()
    last_asst = [m for m in conv["messages"] if m["role"] == "assistant"][-1]
    judgment_blocks = [b for b in last_asst["content_blocks"]
                       if b.get("type") == "task" and b.get("data", {}).get("kind") == "judgment"]
    assert judgment_blocks, f"expected a judgment card, got {last_asst['content_blocks']}"
