"""M4 end-to-end closure integration test — the real task & diagnosis closed loop
(PRODUCTIZATION §M4 core acceptance path).

Drives the FULL misconception lifecycle through the product ``/api/*`` surface
offline, with the Diagnostician as the real judge (NOT bypassed):

  request task (quiz) → answer wrong → SUSPECTED
  → request task (probe) → answer wrong → LIKELY → CONFIRMED
  → request task (changed_task stage 1) → answer correct → VERIFYING
  → request task (changed_task stage 2) → answer correct → RESOLVED
  → request task (probe) → answer wrong → RELAPSED

Throughout: the browser-facing task card never leaks rubric / concept_id /
bug_id / expected_answer; answers go through POST /api/tasks/{id}/answer with
only answer_text + idempotency_key; a repeated idempotency key replays without
re-writing Evidence.

This test runs against a SqlRepository so persistence is real. It exists because
the original test_api_m4.py only asserted the NEEDS_REVIEW / no-write behavior
that held *before* the Diagnostician gained an offline fallback — i.e. it
passed precisely because the closure was dead. This file proves the closure is
alive.
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


def _make_project(client, name="Java 学习"):
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["project_id"]


def _seed_demo(client, pid):
    r = client.post(f"/api/projects/{pid}/books/seed-demo")
    assert r.status_code == 200, r.text


def _new_conversation(client, pid):
    r = client.post(f"/api/projects/{pid}/conversations", json={})
    assert r.status_code == 200
    return r.json()["conversation_id"]


def _request_task(client, cid, text="考考我"):
    """Send a REQUEST_TASK message and return the task card payload."""
    r = client.post(f"/api/conversations/{cid}/messages", json={"content": text})
    assert r.status_code == 200, r.text
    conv = client.get(f"/api/conversations/{cid}").json()
    asst = [m for m in conv["messages"] if m["role"] == "assistant"][-1]
    for b in asst["content_blocks"]:
        if b.get("type") == "task" and b.get("data", {}).get("kind") in (
                "probe", "changed_task", "quiz"):
            return b["data"]
    return None


def _answer(client, task_id, text, key):
    r = client.post(f"/api/tasks/{task_id}/answer",
                    json={"answer_text": text, "idempotency_key": key})
    assert r.status_code == 200, r.text
    return r.json()


def _misconceptions(client, pid):
    r = client.get(f"/api/projects/{pid}/misconceptions")
    assert r.status_code == 200, r.text
    return r.json()


WRONG = "a.getValue() returns the original value because b is a separate copy."
CORRECT = "a.getValue() returns 9 because a and b refer to the same object."


# --- the full closure --------------------------------------------------------

def test_full_misconception_closure_through_product_api(client):
    """SUSPECTED → LIKELY → CONFIRMED → REMEDIATING → VERIFYING → RESOLVED →
    RELAPSED, all via /api/* with the Diagnostician judging for real."""
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid = _new_conversation(client, pid)

    # 1. Cold start → a quiz (no misconception yet to probe).
    t1 = _request_task(client, cid)
    assert t1 is not None and t1["kind"] == "quiz"
    # The card must not leak internal fields.
    for forbidden in ("rubric", "expected_answer", "target_concept_ids",
                      "discriminated_bug_ids"):
        assert forbidden not in t1

    # 2. Wrong quiz answer → SUSPECTED (one ordinary error does not confirm).
    r1 = _answer(client, t1["task_id"], WRONG, "k1")
    assert r1["written"] is True
    assert r1["judgment"]["result"] == "FAIL"
    mis = _misconceptions(client, pid)
    assert mis and mis[0]["status"] == "SUSPECTED"

    # 3. Now an active misconception exists → the next task is a PROBE.
    t2 = _request_task(client, cid)
    assert t2 is not None and t2["kind"] == "probe", f"expected probe, got {t2}"
    r2 = _answer(client, t2["task_id"], WRONG, "k2")
    assert r2["judgment"]["result"] == "FAIL"
    mis = _misconceptions(client, pid)
    assert mis[0]["status"] in ("LIKELY", "CONFIRMED")

    # 4. Another probe wrong answer → CONFIRMED (score≥6, ≥2 tasks, ≥1 probe).
    t3 = _request_task(client, cid)
    assert t3["kind"] == "probe"
    r3 = _answer(client, t3["task_id"], WRONG, "k3")
    assert r3["judgment"]["result"] == "FAIL"
    mis = _misconceptions(client, pid)
    assert mis[0]["status"] == "CONFIRMED", f"expected CONFIRMED, got {mis[0]['status']}"

    # 5. CONFIRMED → REMEDIATE → a changed_task (stage 1). Correct → VERIFYING.
    t4 = _request_task(client, cid)
    assert t4["kind"] == "changed_task", f"expected changed_task, got {t4}"
    assert t4["remediation_stage"] == 1
    r4 = _answer(client, t4["task_id"], CORRECT, "k4")
    assert r4["judgment"]["result"] == "PASS"
    mis = _misconceptions(client, pid)
    assert mis[0]["status"] == "VERIFYING"

    # 6. Second changed_task (stage 2, distinct scenario) → RESOLVED.
    t5 = _request_task(client, cid)
    assert t5["kind"] == "changed_task"
    assert t5["remediation_stage"] == 2
    r5 = _answer(client, t5["task_id"], CORRECT, "k5")
    assert r5["judgment"]["result"] == "PASS"
    mis = _misconceptions(client, pid)
    assert mis[0]["status"] == "RESOLVED", f"expected RESOLVED, got {mis[0]['status']}"
    assert mis[0]["changed_task_pass_count"] == 2

    # 7. A new high-discrimination probe after RESOLVED → RELAPSED.
    t6 = _request_task(client, cid)
    assert t6["kind"] == "probe", f"expected probe for relapse, got {t6}"
    r6 = _answer(client, t6["task_id"], WRONG, "k6")
    assert r6["judgment"]["result"] == "FAIL"
    mis = _misconceptions(client, pid)
    assert mis[0]["status"] == "RELAPSED", f"expected RELAPSED, got {mis[0]['status']}"


def test_one_ordinary_error_does_not_confirm(client):
    """A single quiz FAIL must not reach CONFIRMED (LEARNING_MODEL §8)."""
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid = _new_conversation(client, pid)
    t = _request_task(client, cid)
    _answer(client, t["task_id"], WRONG, "only")
    mis = _misconceptions(client, pid)
    assert mis, "a misconception hypothesis should exist"
    assert mis[0]["status"] != "CONFIRMED"


def test_idempotent_resubmit_does_not_double_write(client):
    """A repeated (task_id, idempotency_key) replays the stored result and does
    not write a second Evidence."""
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid = _new_conversation(client, pid)
    t = _request_task(client, cid)
    r1 = _answer(client, t["task_id"], WRONG, "idem-X")
    assert r1["written"] is True
    r2 = _answer(client, t["task_id"], "a different answer", "idem-X")
    assert r2.get("replay") is True
    assert r2["written"] is False


def test_task_card_never_leaks_internals(client):
    """Every task card surfaced through the product API omits rubric, concept
    ids, bug ids, expected answer — the browser only ever sees the prompt."""
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid = _new_conversation(client, pid)
    seen_kinds = set()
    for i in range(6):
        t = _request_task(client, cid)
        if t is None:
            break
        seen_kinds.add(t["kind"])
        for forbidden in ("rubric", "expected_answer", "target_concept_ids",
                          "discriminated_bug_ids"):
            assert forbidden not in t, f"{t['kind']} card leaked {forbidden}"
        # The task view endpoint is equally safe.
        view = client.get(f"/api/tasks/{t['task_id']}").json()
        for forbidden in ("rubric", "expected_answer", "target_concept_ids",
                          "discriminated_bug_ids"):
            assert forbidden not in view
        # Answer with the appropriate text to advance the closure.
        text = CORRECT if t["kind"] == "changed_task" else WRONG
        _answer(client, t["task_id"], text, f"leak-{i}")
    # The closure exercised quiz, probe, and changed_task kinds.
    assert {"quiz", "probe", "changed_task"} <= seen_kinds
