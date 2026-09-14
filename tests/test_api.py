"""End-to-end API tests — drive the full closed loop over HTTP.

PRODUCT_SPEC §8: "演示路径与真实服务路径一致". Uses FastAPI's TestClient
against the in-memory repo. No live model; the caller supplies the judgment.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bookmind.api.app import create_app, get_repo


@pytest.fixture()
def client():
    # fresh repo + QA service for each test
    import bookmind.api.app as appmod
    appmod._REPO = type(appmod._REPO)()
    appmod._QA_SERVICE = None
    appmod._MAPPING_SERVICE = None
    appmod._LAST_MAPPING = {}
    return TestClient(create_app())


def _setup(client):
    client.post("/users", json={"user_id": "u1", "display_name": "Ada"})
    client.post("/projects", json={"project_id": "p1", "learner_id": "u1", "name": "Java OOP"})
    r = client.post("/projects/p1/books/seed", json={"book_id": "b1", "title": "Java Core"})
    assert r.status_code == 200
    assert r.json()["concepts"] == 30


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_seed_seeds_30_concepts(client):
    _setup(client)
    concepts = client.get("/projects/p1/concepts").json()
    assert len(concepts) == 30
    ids = {c["concept_id"] for c in concepts}
    assert "c_reference" in ids and "c_polymorphism" in ids


def test_next_action_quiet_reading_continues(client):
    _setup(client)
    r = client.post("/projects/p1/next-action", json={
        "activity_mode": "READING", "intervention_policy": "QUIET",
        "ui_preset": "Quiet Reading", "has_active_reading_passage": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["selected_action"] == "CONTINUE_READING"


def test_next_action_assessment_verifies(client):
    _setup(client)
    r = client.post("/projects/p1/next-action", json={
        "activity_mode": "ASSESSMENT", "intervention_policy": "QUIET",
        "ui_preset": "Assessment",
    })
    assert r.json()["selected_action"] == "VERIFY"


def test_submit_answer_independent_pass_verifies_l1(client):
    _setup(client)
    r = client.post("/projects/p1/submit-answer", json={
        "task_id": "t1", "task_version": 1, "target_concept_ids": ["c_reference"],
        "evidence_for_levels": ["L1"], "rubric": ["recalls reference semantics"],
        "result": "PASS", "submission_id": "s1",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["written"] is True
    assert "L1" in body["verified_levels"]
    # state reflects L1 verified
    states = {s["concept_id"]: s for s in client.get("/projects/p1/state").json()}
    assert states["c_reference"]["current_verified_level"] == "L1"
    assert states["c_reference"]["L1"] == "VERIFIED"


def test_submit_answer_hinted_does_not_verify(client):
    _setup(client)
    r = client.post("/projects/p1/submit-answer", json={
        "task_id": "t1", "target_concept_ids": ["c_reference"],
        "evidence_for_levels": ["L1"], "rubric": ["x"],
        "result": "PASS", "hints_issued": 2, "submission_id": "s1",
    })
    body = r.json()
    assert body["written"] is True
    assert body["verified_levels"] == []
    states = {s["concept_id"]: s for s in client.get("/projects/p1/state").json()}
    assert states["c_reference"]["current_verified_level"] == "L0"


def test_event_key_replay_idempotent(client):
    _setup(client)
    payload = {
        "task_id": "t1", "target_concept_ids": ["c_reference"],
        "evidence_for_levels": ["L1"], "rubric": ["x"],
        "result": "PASS", "submission_id": "s1",
    }
    r1 = client.post("/projects/p1/submit-answer", json=payload).json()
    r2 = client.post("/projects/p1/submit-answer", json=payload).json()
    assert r1["written"] is True
    assert r2["written"] is False
    assert "replay" in r2["reason"]


def test_full_misconception_loop(client):
    """End-to-end: errors → CONFIRMED → remediate → 2 changed tasks → RESOLVED."""
    _setup(client)
    sig = [{"bug_id": "bug_ref_vs_object", "direction": "FOR", "strength": "STRONG"}]
    # two STRONG failures from two tasks
    for i in (1, 2):
        client.post("/projects/p1/submit-answer", json={
            "task_id": f"t{i}", "target_concept_ids": ["c_reference"],
            "evidence_for_levels": ["L1"], "rubric": ["x"],
            "result": "FAIL", "misconception_signals": sig, "submission_id": f"s{i}",
        })
    # high-disc probe
    client.post("/projects/p1/submit-answer", json={
        "task_id": "t3", "target_concept_ids": ["c_reference"],
        "evidence_for_levels": ["L1"], "rubric": ["x"],
        "result": "FAIL", "is_probe": True,
        "discriminated_bug_ids": ["bug_ref_vs_object"],
        "misconception_signals": sig, "submission_id": "s3",
    })
    mis = client.get("/projects/p1/misconceptions").json()
    assert mis[0]["status"] == "CONFIRMED"

    # start remediation
    client.post("/projects/p1/start-remediation", json={"bug_id": "bug_ref_vs_object"})
    # first changed task PASS (near transfer)
    client.post("/projects/p1/submit-answer", json={
        "task_id": "ct1", "target_concept_ids": ["c_reference"],
        "evidence_for_levels": ["L2"], "rubric": ["x"],
        "result": "PASS", "is_changed_task": True,
        "scenario_fingerprint": "scene_A",
        "discriminated_bug_ids": ["bug_ref_vs_object"], "submission_id": "sct1",
    })
    mis = client.get("/projects/p1/misconceptions").json()
    assert mis[0]["status"] == "VERIFYING"
    # second changed task PASS (far transfer, distinct scenario)
    client.post("/projects/p1/submit-answer", json={
        "task_id": "ct2", "target_concept_ids": ["c_reference"],
        "evidence_for_levels": ["L2"], "rubric": ["x"],
        "result": "PASS", "is_changed_task": True,
        "scenario_fingerprint": "scene_B",
        "discriminated_bug_ids": ["bug_ref_vs_object"], "submission_id": "sct2",
    })
    mis = client.get("/projects/p1/misconceptions").json()
    assert mis[0]["status"] == "RESOLVED"


def test_cross_project_state_isolation(client):
    """Different projects do not share learning state (LEARNING_MODEL §1.1).

    c_reference exists under both b1 (p1) and b2 (p2) — same skeleton id, but
    they are *different* concept instances. Evidence written to p1 must not
    raise mastery of c_reference in p2.
    """
    _setup(client)  # u1/p1/b1
    client.post("/users", json={"user_id": "u2"})
    client.post("/projects", json={"project_id": "p2", "learner_id": "u2", "name": "Other"})
    client.post("/projects/p2/books/seed", json={"book_id": "b2"})

    # u1 verifies c_reference in p1 → p1 state rises to L1.
    client.post("/projects/p1/submit-answer", json={
        "task_id": "t1", "target_concept_ids": ["c_reference"],
        "evidence_for_levels": ["L1"], "rubric": ["x"],
        "result": "PASS", "submission_id": "s1",
    })
    p1_states = {s["concept_id"]: s for s in client.get("/projects/p1/state").json()}
    assert p1_states["c_reference"]["current_verified_level"] == "L1"

    # p2's c_reference must remain L0 — no cross-project mastery transfer.
    p2_states = {s["concept_id"]: s for s in client.get("/projects/p2/state").json()}
    assert p2_states["c_reference"]["current_verified_level"] == "L0"

    # And p2 has no evidence/misconceptions from p1's submission.
    assert client.get("/projects/p2/misconceptions").json() == []


# --- Phase 2: ingestion & textbook Q&A ----------------------------------

import base64

_QA_PDF = (
    b"%PDF-1.4 1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj "
    b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj "
    b"3 0 obj<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>endobj "
    b"4 0 obj<< /Length 120 >>stream\nBT /F1 12 Tf 72 700 Td (4.1 References and Objects) Tj "
    b"0 -14 Td (A reference variable stores the address of an object, not the object itself.) Tj "
    b"0 -14 Td (Using == compares references; equals compares content.) Tj ET\nendstream endobj"
)


def _qa_setup(client):
    client.post("/users", json={"user_id": "u1"})
    client.post("/projects", json={"project_id": "p1", "learner_id": "u1", "name": "Java"})


def test_ingest_then_ask(client):
    _qa_setup(client)
    r = client.post("/projects/p1/ingest", json={
        "learner_id": "u1", "book_id": "b1", "filename": "book.pdf", "title": "Java Core",
        "content_base64": base64.b64encode(_QA_PDF).decode(),
    })
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "SUCCEEDED"
    assert body["chunks"] > 0

    chunks = client.get("/projects/p1/chunks").json()
    assert len(chunks) > 0
    assert all("page" in c for c in chunks)

    a = client.post("/projects/p1/ask", json={
        "learner_id": "u1", "question": "引用和对象的区别",
    }).json()
    assert a["grounded"] is True
    assert a["chunk_ids"]


def test_ask_before_ingest(client):
    _qa_setup(client)
    a = client.post("/projects/p1/ask", json={
        "learner_id": "u1", "question": "anything",
    }).json()
    assert a["grounded"] is False
    assert "ingest" in a["reason"]


def test_ingest_rejects_wrong_learner(client):
    _qa_setup(client)
    r = client.post("/projects/p1/ingest", json={
        "learner_id": "u2", "book_id": "b1",
        "content_base64": base64.b64encode(_QA_PDF).decode(),
    })
    assert r.status_code == 403


def test_seed_demo_then_ask(client):
    """The offline demo corpus seeds without any upload and supports Q&A."""
    _qa_setup(client)
    r = client.post("/projects/p1/seed-demo", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["concepts"] == 30
    assert body["chunks"] > 0
    a = client.post("/projects/p1/ask", json={
        "learner_id": "u1", "question": "== 和 equals 的区别",
    }).json()
    assert a["grounded"] is True
    assert a["chunk_ids"]
    # concepts are also seeded from the gold skeleton
    concepts = client.get("/projects/p1/concepts").json()
    assert len(concepts) == 30


# --- Phase 3: Book Mapping API ------------------------------------------

def test_map_book_extends_concepts(client):
    """seed-demo (gold 30) → map-book extends to 50–80, gold protected."""
    _qa_setup(client)
    client.post("/projects/p1/seed-demo", json={})
    r = client.post("/projects/p1/map-book", json={
        "learner_id": "u1", "book_id": "demo_java_core", "graph_key": "g1",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["gold_concepts"] == 30
    assert body["total_concepts"] >= 50
    assert body["new_concepts"] == body["total_concepts"] - 30
    # No edges were dropped to cycles (offline path is clean).
    assert body["dropped_edges"] == []


def test_map_book_idempotent(client):
    _qa_setup(client)
    client.post("/projects/p1/seed-demo", json={})
    client.post("/projects/p1/map-book", json={
        "learner_id": "u1", "book_id": "demo_java_core", "graph_key": "g1",
    })
    r2 = client.post("/projects/p1/map-book", json={
        "learner_id": "u1", "book_id": "demo_java_core", "graph_key": "g1",
    })
    assert r2.json()["reused"] is True


def test_map_book_rejects_wrong_learner(client):
    _qa_setup(client)
    client.post("/projects/p1/seed-demo", json={})
    client.post("/users", json={"user_id": "u2"})
    r = client.post("/projects/p1/map-book", json={
        "learner_id": "u2", "book_id": "demo_java_core", "graph_key": "g1",
    })
    assert r.status_code == 403


def test_map_book_rejects_out_of_scope_book(client):
    _qa_setup(client)
    client.post("/projects/p1/seed-demo", json={})
    r = client.post("/projects/p1/map-book", json={
        "learner_id": "u1", "book_id": "other_book", "graph_key": "g1",
    })
    assert r.status_code == 403


def test_undo_mapping_restores_gold_only(client):
    _qa_setup(client)
    client.post("/projects/p1/seed-demo", json={})
    mb = client.post("/projects/p1/map-book", json={
        "learner_id": "u1", "book_id": "demo_java_core", "graph_key": "g1",
    }).json()
    r = client.post("/projects/p1/undo-mapping", json={
        "learner_id": "u1", "book_id": "demo_java_core",
    })
    assert r.status_code == 200
    res = r.json()
    assert res["removed_concepts"] == mb["new_concepts"]
    # Only the 30 gold concepts remain.
    concepts = client.get("/projects/p1/concepts").json()
    assert len(concepts) == 30
    assert all(c["source"] == "GOLD" for c in concepts)


def test_undo_without_mapping_is_404(client):
    _qa_setup(client)
    client.post("/projects/p1/seed-demo", json={})
    r = client.post("/projects/p1/undo-mapping", json={
        "learner_id": "u1", "book_id": "demo_java_core",
    })
    assert r.status_code == 404


def test_book_graph_endpoint_groups_by_chapter(client):
    _qa_setup(client)
    client.post("/projects/p1/seed-demo", json={})
    client.post("/projects/p1/map-book", json={
        "learner_id": "u1", "book_id": "demo_java_core", "graph_key": "g1",
    })
    r = client.get("/projects/p1/book-graph?book_id=demo_java_core")
    assert r.status_code == 200
    body = r.json()
    assert body["total_concepts"] >= 50
    assert isinstance(body["chapters"], dict)
    assert len(body["chapters"]) >= 5  # multiple chapters
    assert isinstance(body["edges"], list)
    # Gold prerequisite edge present (c_reference → c_variable).
    edge_pairs = {(e["source"], e["target"]) for e in body["edges"]}
    assert ("c_reference", "c_variable") in edge_pairs


# --- Phase 4: Learner Model & mode presets --------------------------------

def test_exposure_endpoint_moves_exposure_not_mastery(client):
    _setup(client)
    r = client.post("/projects/p1/exposure", json={
        "learner_id": "u1", "concept_id": "c_reference", "evidence_type": "READ",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["exposure_state"] == "SEEN"
    # mastery untouched
    states = {s["concept_id"]: s for s in client.get("/projects/p1/state").json()}
    assert states["c_reference"]["current_verified_level"] == "L0"


def test_exposure_coverage_completes(client):
    _setup(client)
    r = client.post("/projects/p1/exposure", json={
        "learner_id": "u1", "concept_id": "c_reference",
        "evidence_type": "READ", "read_coverage": 0.95,
    })
    assert r.json()["exposure_state"] == "COMPLETED"


def test_exposure_rejects_verify_type(client):
    _setup(client)
    r = client.post("/projects/p1/exposure", json={
        "learner_id": "u1", "concept_id": "c_reference", "evidence_type": "VERIFY",
    })
    assert r.status_code == 400


def test_learning_state_expands_view(client):
    """The Learning State page shows derived effective status, retrievability,
    the evidence chain, and a group bucket — not just raw levels."""
    _setup(client)
    # Verify L1 on c_reference.
    client.post("/projects/p1/submit-answer", json={
        "task_id": "t1", "target_concept_ids": ["c_reference"],
        "evidence_for_levels": ["L1"], "rubric": ["x"],
        "result": "PASS", "submission_id": "s1",
    })
    views = {v["concept_id"]: v for v in client.get("/projects/p1/learning-state").json()}
    v = views["c_reference"]
    assert v["current_verified_level"] == "L1"
    assert v["group"] == "verified"
    l1 = next(lv for lv in v["levels"] if lv["level"] == "L1")
    assert l1["effective_status"] == "VERIFIED"
    assert l1["retrievability"] > 0.0
    assert len(v["evidence"]) >= 1
    assert v["evidence"][0]["evidence_type"] == "VERIFY"
