"""Phase 5 API tests — misconception closure endpoints over HTTP."""

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


def _setup(client):
    client.post("/users", json={"user_id": "u1"})
    client.post("/projects", json={"project_id": "p1", "learner_id": "u1", "name": "Java"})
    client.post("/projects/p1/books/seed", json={"book_id": "b1"})


def test_classify_probe_endpoint(client):
    _setup(client)
    r = client.post("/projects/p1/classify-probe", json={
        "bug_id": "bug_ref_vs_object",
        "answer_text": "a.getValue() returns the original value because b is a separate copy.",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["best_hypothesis"] == "h_value_semantics"
    assert body["method"] == "keyword"


def test_classify_probe_unknown_bug(client):
    _setup(client)
    r = client.post("/projects/p1/classify-probe", json={
        "bug_id": "bug_nope", "answer_text": "whatever",
    })
    assert r.status_code == 404


def test_validate_task_endpoint_passes(client):
    _setup(client)
    r = client.post("/projects/p1/validate-task", json={
        "task_id": "probe1", "target_concept_ids": ["c_reference"],
        "evidence_for_levels": ["L2"], "rubric": ["identifies aliasing"],
        "prompt_text": "Given Box a = new Box(1); Box b = a; b.setValue(9); what is a.getValue() and why?",
        "is_probe": True, "discriminated_bug_ids": ["bug_ref_vs_object"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    assert body["trusted"] is not None


def test_validate_task_endpoint_rejects_bad_schema(client):
    _setup(client)
    r = client.post("/projects/p1/validate-task", json={
        "task_id": "probe1", "target_concept_ids": [],
        "evidence_for_levels": ["L2"], "rubric": ["x"],
        "prompt_text": "q?", "is_probe": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    assert any("schema" in b for b in body["blocked_reasons"])


def test_probe_endpoint_generates_valid_task(client):
    _setup(client)
    r = client.post("/projects/p1/probe", json={"bug_id": "bug_ref_vs_object"})
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    assert body["trusted"]["is_probe"] is True
    assert body["trusted"]["discriminated_bug_ids"] == ["bug_ref_vs_object"]


def test_changed_task_endpoint(client):
    _setup(client)
    r = client.post("/projects/p1/changed-task", json={"bug_id": "bug_ref_vs_object", "stage": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    assert body["trusted"]["is_changed_task"] is True
    assert body["trusted"]["remediation_stage"] == 1


def test_changed_task_rejects_bad_stage(client):
    _setup(client)
    r = client.post("/projects/p1/changed-task", json={"bug_id": "bug_ref_vs_object", "stage": 3})
    assert r.status_code == 400


def test_misconception_trace_endpoint(client):
    _setup(client)
    # No misconception yet → empty trace.
    r = client.get("/projects/p1/misconceptions/bug_ref_vs_object/trace")
    assert r.status_code == 200
    body = r.json()
    assert body["bug_id"] == "bug_ref_vs_object"
    assert body["evidence_score"] == 0
    assert body["evidence_chain"] == []


def test_remediation_start_requires_confirmed(client):
    _setup(client)
    # No misconception record → 404.
    r = client.post("/projects/p1/remediation/start", json={"bug_id": "bug_ref_vs_object"})
    assert r.status_code == 404


def test_remediation_start_unknown_bug(client):
    _setup(client)
    r = client.post("/projects/p1/remediation/start", json={"bug_id": "bug_nope"})
    assert r.status_code == 404


def test_full_closure_via_api(client):
    """End-to-end over HTTP: confirm a bug, remediate, resolve, check trace."""
    _setup(client)
    # Drive to CONFIRMED with 3 submissions (2 verify FAIL + 1 probe FAIL).
    for i in range(2):
        client.post("/projects/p1/submit-answer", json={
            "task_id": f"t{i}", "task_version": 1, "target_concept_ids": ["c_reference"],
            "evidence_for_levels": ["L2"], "rubric": ["x"], "result": "FAIL",
            "submission_id": f"s{i}",
            "misconception_signals": [{"bug_id": "bug_ref_vs_object", "direction": "FOR", "strength": "STRONG"}],
        })
    # Probe with no signals → classifier fills FOR.
    client.post("/projects/p1/submit-answer", json={
        "task_id": "probe1", "task_version": 1, "target_concept_ids": ["c_reference"],
        "evidence_for_levels": ["L2"], "rubric": ["x"], "result": "FAIL",
        "is_probe": True, "discriminated_bug_ids": ["bug_ref_vs_object"],
        "answer_text": "a.getValue() returns the original value because b is a separate copy.",
        "submission_id": "sp",
    })
    mis = client.get("/projects/p1/misconceptions").json()
    bug = next(m for m in mis if m["bug_id"] == "bug_ref_vs_object")
    assert bug["status"] == "CONFIRMED"

    # Start remediation.
    r = client.post("/projects/p1/remediation/start", json={"bug_id": "bug_ref_vs_object"})
    assert r.status_code == 200
    plan = r.json()
    assert plan["explanation_goal"]
    assert plan["changed_task_stages"] == [1, 2]

    # Two changed-task PASS.
    for stage, fp in [(1, "scene_A"), (2, "scene_B")]:
        client.post("/projects/p1/submit-answer", json={
            "task_id": f"ct{stage}", "task_version": 1, "target_concept_ids": ["c_reference"],
            "evidence_for_levels": ["L3"], "rubric": ["x"], "result": "PASS",
            "is_changed_task": True, "discriminated_bug_ids": ["bug_ref_vs_object"],
            "scenario_fingerprint": fp, "submission_id": f"cts{stage}",
        })
    trace = client.get("/projects/p1/misconceptions/bug_ref_vs_object/trace").json()
    assert trace["status"] == "RESOLVED"
    assert trace["changed_task_pass_count"] == 2
    assert len(trace["evidence_chain"]) >= 5  # 3 confirm + 2 changed
