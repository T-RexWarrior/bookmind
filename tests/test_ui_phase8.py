"""Phase 8 UI / demo smoke tests — ROADMAP Phase 8.

Verifies:
  - the static frontend is served at /ui (ARCHITECTURE §13 fallback);
  - the full offline closed loop runs over HTTP end-to-end: seed → ask →
    submit-answer → next-action → learning-state → recovery (the 5-minute
    demo script, automated);
  - CORS is enabled for local development.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bookmind.api.app import create_app


@pytest.fixture()
def client():
    import bookmind.api.app as appmod
    appmod._REPO = type(appmod._REPO)()
    appmod._QA_SERVICE = None
    appmod._MAPPING_SERVICE = None
    appmod._LAST_MAPPING = {}
    return TestClient(create_app())


def test_frontend_served_at_ui(client):
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "BookMind" in r.text or "学迹" in r.text
    # M5: the product UI is a built React app; the built index.html loads the
    # Vite bundle from /ui/assets/*. The legacy vanilla ESM entry is gone.
    assert "/ui/assets/" in r.text or "/src/main" in r.text
    # Legacy debug console still reachable for development.
    legacy = client.get("/ui/legacy-debug.html")
    assert legacy.status_code == 200
    for view in ("Reader", "Learning State", "Recovery", "误区诊断"):
        assert view in legacy.text


def test_ui_spa_fallback_for_client_routes(client):
    """A refresh on a client-side route like /ui/projects/x serves index.html,
    not a 404 (PRODUCTIZATION M5 SPA fallback)."""
    r = client.get("/ui/projects/some-id")
    assert r.status_code == 200
    assert "学迹" in r.text or "BookMind" in r.text


def test_cors_header_present(client):
    # A preflight request should get CORS headers back.
    r = client.options("/health", headers={
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "GET",
    })
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "*"


def test_full_offline_closed_loop_over_http(client):
    """The 5-minute demo script, automated over HTTP: seed → ask → verify →
    next-action → learning-state → recovery. All offline."""
    # 1. Setup + seed demo.
    client.post("/users", json={"user_id": "u1"})
    client.post("/projects", json={"project_id": "p1", "learner_id": "u1", "name": "Java"})
    seed = client.post("/projects/p1/seed-demo").json()
    assert seed["concepts"] > 0 and seed["chunks"] > 0

    # 2. Textbook Q&A (offline RAG).
    ans = client.post("/projects/p1/ask", json={
        "learner_id": "u1", "question": "== 和 equals 的区别", "top_k": 3,
    }).json()
    assert "answer_text" in ans

    # 3. Submit an independent L1 PASS.
    res = client.post("/projects/p1/submit-answer", json={
        "task_id": "verify_l1", "target_concept_ids": ["c_reference"],
        "evidence_for_levels": ["L1"], "rubric": ["recall"],
        "result": "PASS", "submission_id": "s1",
    }).json()
    assert res["written"] is True
    assert "L1" in res["verified_levels"]

    # 4. Next action.
    na = client.post("/projects/p1/next-action", json={
        "activity_mode": "READING", "intervention_policy": "PROACTIVE",
        "ui_preset": "Deep Learning",
    }).json()
    assert "selected_action" in na

    # 5. Learning State page.
    state = client.get("/projects/p1/learning-state").json()
    assert len(state) == seed["concepts"]
    groups = {v["group"] for v in state}
    assert "verified" in groups  # c_reference is now verified

    # 6. Recovery page (no candidates yet — nothing expired).
    rec = client.get("/projects/p1/recovery").json()
    assert rec["recommendation"] == "continue"

    # 7. Misconceptions (empty initially).
    mis = client.get("/projects/p1/misconceptions").json()
    assert mis == []


def test_health_endpoint_reports_version(client):
    r = client.get("/health").json()
    assert r["status"] == "ok"
    assert "version" in r
