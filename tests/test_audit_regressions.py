"""Regression tests for the 2026-09-05 audit findings.

Each test pins one reported defect so the fix cannot silently regress:

  P0-01: an irrelevant long answer is NOT judged PASS offline (NEEDS_REVIEW).
  P0-02: two users requesting a task get distinct task_ids (no overwrite).
  P0-05: a version conflict on save_state raises ConcurrentWriteError; evidence
         + state commit atomically inside one transaction.
  P1-10: in ASSESSMENT mode a textbook question does not return excerpts.
  P1-12: a repeated (conversation_id, idempotency_key) replays, not duplicates.
  P1-14: a help cue while a task is pending is NOT judged as an answer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bookmind.agents.diagnostician import DiagnosticianAgent
from bookmind.domain.enums import EvidenceResult, JudgmentStatus, Level
from bookmind.domain.models import TrustedTaskContext
from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.llm.schemas import ModelResult
from bookmind.storage.sql import SqlRepository


# --- P0-01: irrelevant long answer must not PASS offline -------------------

class _FailRouter(ModelRouter):
    """A router that always fails → forces the offline judge path."""

    def __init__(self):
        super().__init__(RouterConfig(live=False))

    def complete(self, task, messages, *, output_schema=None, temperature=None, max_tokens=None):
        return ModelResult(ok=False, task=task, model="fail", content=None,
                           parsed_json=None, error="offline", fallback=True)


def test_p0_01_irrelevant_long_answer_not_judged_pass():
    """Audit P0-01: '今天天气很好我想出去散步' must NOT be PASS. The offline judge
    cannot confirm a free-text answer correct, so it returns NEEDS_REVIEW and
    the Engine never raises mastery on a guess."""
    d = DiagnosticianAgent(_FailRouter())
    task = TrustedTaskContext(
        task_id="t", task_version=1, target_concept_ids=["c_reference"],
        evidence_for_levels=[Level.L1], rubric=["explains references"],
    )
    j = d.judge(task, "今天天气很好我想出去散步走一走呼吸新鲜空气")
    assert j.judgment_status == JudgmentStatus.NEEDS_REVIEW
    assert j.result is None


def test_p0_01_repeated_chars_not_judged_pass():
    """Audit P0-01: a long run of repeated characters is not a substantive answer."""
    d = DiagnosticianAgent(_FailRouter())
    task = TrustedTaskContext(
        task_id="t", task_version=1, target_concept_ids=["c_reference"],
        evidence_for_levels=[Level.L1], rubric=["explains references"],
    )
    j = d.judge(task, "啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊")
    assert j.judgment_status == JudgmentStatus.NEEDS_REVIEW


def test_p0_01_wrong_answer_still_fails():
    """The fix must not break wrong-answer detection: a known wrong pattern FAILs."""
    d = DiagnosticianAgent(_FailRouter())
    task = TrustedTaskContext(
        task_id="t", task_version=1, target_concept_ids=["c_reference"],
        evidence_for_levels=[Level.L1], rubric=["explains references"],
    )
    j = d.judge(task, "a and b are separate copies with the original value")
    assert j.judgment_status == JudgmentStatus.DECIDED
    assert j.result == EvidenceResult.FAIL


# --- P0-02 + P1-10 + P1-12 + P1-14: product API (SqlRepository) ------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKMIND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOOKMIND_DATABASE_URL", f"sqlite:///{tmp_path}/bm.db")
    from bookmind.config import get_settings
    get_settings.cache_clear()
    repo = SqlRepository(f"sqlite:///{tmp_path}/bm.db")
    repo.create_schema()
    from bookmind.api.app import create_app
    app = create_app(repo=repo)
    with TestClient(app) as c:
        yield c


def _bootstrap(client):
    client.post("/api/session/bootstrap")


def _make_project(client, name="p"):
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
    r = client.post(f"/api/conversations/{cid}/messages", json={"content": text})
    assert r.status_code == 200, r.text
    conv = client.get(f"/api/conversations/{cid}").json()
    asst = [m for m in conv["messages"] if m["role"] == "assistant"][-1]
    for b in asst["content_blocks"]:
        if b.get("type") == "task" and b.get("data", {}).get("kind") in ("probe", "changed_task", "quiz"):
            return b["data"]
    return None


def test_p0_02_two_users_distinct_task_ids(client):
    """Audit P0-02: two users each request a task; the task_ids must differ so
    neither overwrites the other's task row."""
    from fastapi.testclient import TestClient
    _bootstrap(client)
    pid_a = _make_project(client, "A")
    _seed_demo(client, pid_a)
    cid_a = _new_conversation(client, pid_a)
    task_a = _request_task(client, cid_a)
    assert task_a is not None

    client_b = TestClient(client.app)
    client_b.post("/api/session/bootstrap")
    pid_b = _make_project(client_b, "B")
    _seed_demo(client_b, pid_b)
    cid_b = _new_conversation(client_b, pid_b)
    task_b = _request_task(client_b, cid_b)
    assert task_b is not None

    assert task_a["task_id"] != task_b["task_id"]
    # A's task must still be readable by A (not clobbered by B).
    r = client.get(f"/api/tasks/{task_a['task_id']}")
    assert r.status_code == 200


def test_p0_05_version_conflict_raises(client):
    """Audit P0-05: saving a state whose version does not match the DB raises
    ConcurrentWriteError instead of silently overwriting."""
    from bookmind.domain.enums import UIPreset
    from bookmind.domain.models import User, LearningProject, Book, ProjectBook, LearnerConceptState
    from bookmind.domain.enums import BookRole
    from bookmind.storage.sql.repository import ConcurrentWriteError
    from bookmind.api.app import create_app

    repo = client.app.state.repo
    repo.add_user(User(user_id="u"))
    repo.create_project(LearningProject(project_id="p", learner_id="u", name="t",
                                        default_mode=UIPreset.QUIET_READING))
    repo.add_book(Book(book_id="b", owner_user_id="u", source_hash="h", title="T"))
    repo.link_book(ProjectBook(project_id="p", book_id="b", role=BookRole.PRIMARY))

    s = repo.get_state("p", "c1")
    s.bump_version()
    repo.save_state(s)  # version 1
    # Simulate a stale write: same version 1 again (as if read before the bump).
    s_stale = repo.get_state("p", "c1")
    s_stale.read_progress = 0.1
    s_stale.bump_version()  # version 2, but DB is already at 1 from a *different* write
    # Force the DB to a newer version to model a concurrent write.
    newer = repo.get_state("p", "c1")
    newer.read_progress = 0.8
    newer.bump_version()  # version 2
    repo.save_state(newer)  # DB now at version 2
    # Now the stale copy (expects db=1) must conflict.
    with pytest.raises(ConcurrentWriteError):
        repo.save_state(s_stale)


def test_p1_10_assessment_mode_blocks_textbook_lookup(client):
    """Audit P1-10: in ASSESSMENT mode a textbook question returns no excerpts
    or page citations (the answer is not leaked)."""
    from bookmind.domain.enums import UIPreset
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    # Switch to assessment mode.
    r = client.patch(f"/api/projects/{pid}", json={"default_mode": "Assessment"})
    assert r.status_code == 200, r.text
    cid = _new_conversation(client, pid)
    r = client.post(f"/api/conversations/{cid}/messages",
                    json={"content": "引用和对象的区别是什么？"})
    assert r.status_code == 200, r.text
    conv = client.get(f"/api/conversations/{cid}").json()
    asst = [m for m in conv["messages"] if m["role"] == "assistant"][-1]
    # No citation blocks and no page numbers leaked.
    citations = [b for b in asst["content_blocks"] if b.get("type") == "citation"]
    assert citations == [], "assessment mode leaked a textbook citation"
    text = " ".join(b.get("text", "") for b in asst["content_blocks"] if b.get("type") == "text")
    assert "评估模式" in text, f"expected an assessment-mode notice, got: {text}"


def test_p1_12_idempotent_send_replays(client):
    """Audit P1-12: sending the same content with the same idempotency_key
    replays the prior run instead of creating a duplicate."""
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid = _new_conversation(client, pid)
    body = {"content": "第一章主要讲什么？", "idempotency_key": "idem-fix-1"}
    r1 = client.post(f"/api/conversations/{cid}/messages", json=body)
    assert r1.status_code == 200
    run1 = r1.json()["run_id"]
    r2 = client.post(f"/api/conversations/{cid}/messages", json=body)
    assert r2.status_code == 200
    assert r2.json()["run_id"] == run1, "idempotent resubmit created a new run"
    assert r2.json().get("replay") is True


def test_p1_14_help_cue_not_judged_as_answer(client):
    """Audit P1-14: with a pending task, '我不懂，先解释一下' is NOT routed to
    the judge (SUBMIT_ANSWER); it is handled as an explanation request."""
    _bootstrap(client)
    pid = _make_project(client)
    _seed_demo(client, pid)
    cid = _new_conversation(client, pid)
    task = _request_task(client, cid)
    assert task is not None
    r = client.post(f"/api/conversations/{cid}/messages",
                    json={"content": "我不懂，先解释一下"})
    assert r.status_code == 200, r.text
    conv = client.get(f"/api/conversations/{cid}").json()
    last_asst = [m for m in conv["messages"] if m["role"] == "assistant"][-1]
    # The reply must NOT be a judgment card.
    judgment = [b for b in last_asst["content_blocks"]
                if b.get("type") == "task" and b.get("data", {}).get("kind") == "judgment"]
    assert not judgment, "a help request was incorrectly judged as an answer"
