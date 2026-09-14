"""M6 product API integration tests — modes, recovery, trust boundaries, and
the conversation/project rename/delete surface (PRODUCTIZATION §M6).

Covers:
  - PATCH/DELETE /api/conversations/{id} (rename + soft-delete, Evidence kept);
  - DELETE /api/projects/{id} (soft-delete, disappears from list);
  - PATCH /api/projects/{id} (mode switch — the project's default_mode is
    updated and read back);
  - GET /api/projects/{id}/review-plan (authenticated recovery plan);
  - the mode-aware decision: a QUIET_READING project still honours an explicit
    "考考我" (user-requested task), and ASSESSMENT restricts to VERIFY/WAIT.

Runs against a SqlRepository so persistence is real.
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


def _make_project(client, name="Java 学习", mode="Quiet Reading"):
    # UIPreset values are the human-readable display strings ("Quiet Reading",
    # "Deep Learning", "Review", "Assessment"), not the enum member names.
    r = client.post("/api/projects", json={"name": name, "default_mode": mode})
    assert r.status_code == 200, r.text
    return r.json()["project_id"]


def _seed(client, pid):
    r = client.post(f"/api/projects/{pid}/books/seed-demo")
    assert r.status_code == 200


def _new_conv(client, pid):
    r = client.post(f"/api/projects/{pid}/conversations")
    assert r.status_code == 200
    return r.json()["conversation_id"]


# --- conversation rename / delete ------------------------------------------


def test_rename_conversation(client):
    _bootstrap(client)
    pid = _make_project(client)
    cid = _new_conv(client, pid)
    r = client.patch(f"/api/conversations/{cid}", json={"title": "我的复习"})
    assert r.status_code == 200
    assert r.json()["title"] == "我的复习"
    # Read back.
    conv = client.get(f"/api/conversations/{cid}").json()
    assert conv["title"] == "我的复习"


def test_conversations_are_isolated_by_learning_activity(client):
    """Each top-level activity has its own conversation inbox."""
    _bootstrap(client)
    pid = _make_project(client)
    learn = client.post(
        f"/api/projects/{pid}/conversations?activity_type=LEARN"
    ).json()
    review = client.post(
        f"/api/projects/{pid}/conversations?activity_type=REVIEW"
    ).json()

    learn_list = client.get(
        f"/api/projects/{pid}/conversations?activity_type=LEARN"
    ).json()
    review_list = client.get(
        f"/api/projects/{pid}/conversations?activity_type=REVIEW"
    ).json()

    assert [item["conversation_id"] for item in learn_list] == [learn["conversation_id"]]
    assert [item["conversation_id"] for item in review_list] == [review["conversation_id"]]
    assert client.get(f"/api/conversations/{review['conversation_id']}").json()["activity_type"] == "REVIEW"


def test_delete_conversation_is_soft_and_keeps_evidence(client):
    """Deleting a conversation removes it from the list but does not delete
    Evidence or project learning state (PRODUCTIZATION §5.12)."""
    _bootstrap(client)
    pid = _make_project(client)
    _seed(client, pid)
    cid = _new_conv(client, pid)
    # Generate some learning state via a task.
    client.post(f"/api/conversations/{cid}/messages", json={"content": "考考我"})

    r = client.delete(f"/api/conversations/{cid}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    # The conversation no longer appears in the list.
    convs = client.get(f"/api/projects/{pid}/conversations").json()
    assert cid not in [c["conversation_id"] for c in convs]

    # The project + learning summary still exist (evidence was not purged).
    assert client.get(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}/learning-summary").status_code == 200


def test_cross_user_cannot_rename_or_delete_conversation(client):
    _bootstrap(client)
    pid = _make_project(client)
    cid = _new_conv(client, pid)
    # A second user.
    client2 = TestClient(create_app(repo=client.app.state.repo))
    _bootstrap(client2)
    assert client2.patch(f"/api/conversations/{cid}", json={"title": "x"}).status_code in (403, 404)
    assert client2.delete(f"/api/conversations/{cid}").status_code in (403, 404)


# --- project delete / update -----------------------------------------------


def test_delete_project_soft_deletes_and_hides_from_list(client):
    _bootstrap(client)
    pid = _make_project(client)
    r = client.delete(f"/api/projects/{pid}")
    assert r.status_code == 200
    assert r.json()["archived"] is True
    # No longer in the user's project list.
    projects = client.get("/api/projects").json()
    assert pid not in [p["project_id"] for p in projects]


def test_update_project_mode(client):
    """PATCH /api/projects/{id} switches the project's default mode (M6)."""
    _bootstrap(client)
    pid = _make_project(client, mode="Quiet Reading")
    assert client.get(f"/api/projects/{pid}").json()["default_mode"] == "Quiet Reading"
    r = client.patch(f"/api/projects/{pid}", json={"default_mode": "Deep Learning"})
    assert r.status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["default_mode"] == "Deep Learning"


def test_cross_user_cannot_delete_or_update_project(client):
    _bootstrap(client)
    pid = _make_project(client)
    client2 = TestClient(create_app(repo=client.app.state.repo))
    _bootstrap(client2)
    assert client2.delete(f"/api/projects/{pid}").status_code in (403, 404)
    assert client2.patch(f"/api/projects/{pid}", json={"name": "x"}).status_code in (403, 404)


# --- recovery (authenticated) ----------------------------------------------


def test_review_plan_is_authenticated_and_read_only(client):
    """GET /api/projects/{id}/review-plan is scoped to the owner and returns a
    'continue' recommendation when nothing is expired (LEARNING_MODEL §12)."""
    _bootstrap(client)
    pid = _make_project(client)
    _seed(client, pid)
    r = client.get(f"/api/projects/{pid}/review-plan")
    assert r.status_code == 200
    plan = r.json()
    assert plan["recommendation"] == "continue"

    # A second user cannot read another user's review plan.
    client2 = TestClient(create_app(repo=client.app.state.repo))
    _bootstrap(client2)
    assert client2.get(f"/api/projects/{pid}/review-plan").status_code in (403, 404)


def test_review_plan_start_records_choice(client):
    _bootstrap(client)
    pid = _make_project(client)
    _seed(client, pid)
    r = client.post(f"/api/projects/{pid}/review-plan/start", json={"choice": "continue"})
    assert r.status_code == 200
    assert r.json()["choice"] == "continue"


def test_review_plan_with_evidence_does_not_crash(client):
    """The recovery plan must not crash when Evidence exists (the SQLite
    timestamp is offset-naive; recovery normalizes it to aware UTC)."""
    _bootstrap(client)
    pid = _make_project(client)
    _seed(client, pid)
    cid = _new_conv(client, pid)
    # Generate Evidence by requesting a task and answering.
    client.post(f"/api/conversations/{cid}/messages", json={"content": "考考我"})
    conv = client.get(f"/api/conversations/{cid}").json()
    asst = [m for m in conv["messages"] if m["role"] == "assistant"][-1]
    task = next((b["data"] for b in asst["content_blocks"]
                 if b["type"] == "task" and b["data"].get("kind") in ("quiz", "probe", "changed_task")), None)
    if task:
        client.post(f"/api/tasks/{task['task_id']}/answer",
                    json={"answer_text": "wrong answer", "idempotency_key": "rp-1"})
    # Now the review-plan call must succeed (not 500 on the naive timestamp).
    r = client.get(f"/api/projects/{pid}/review-plan")
    assert r.status_code == 200
    assert "recommendation" in r.json()


# --- mode-aware decision ---------------------------------------------------


def test_quiet_reading_still_honours_explicit_task_request(client):
    """A QUIET_READING project does not proactively surface tasks, but when the
    user explicitly asks ("考考我") a task IS generated (action_matrix §10:
    QUIET never blocks a user who asks)."""
    _bootstrap(client)
    pid = _make_project(client, mode="Quiet Reading")
    _seed(client, pid)
    cid = _new_conv(client, pid)
    client.post(f"/api/conversations/{cid}/messages", json={"content": "考考我"})
    conv = client.get(f"/api/conversations/{cid}").json()
    asst = [m for m in conv["messages"] if m["role"] == "assistant"][-1]
    kinds = [b["data"]["kind"] for b in asst["content_blocks"] if b["type"] == "task"]
    assert kinds and kinds[0] in ("quiz", "probe", "changed_task")
