import asyncio
import hashlib
import sys
import time
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import learning, llm, store as db
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA", tmp_path)
    (tmp_path / "uploads").mkdir()
    llm.save_settings({"api_key": "sk-unit-test-private-key"})
    with TestClient(app) as c:
        yield c


def space(client, title="测试空间"):
    return client.post("/api/spaces", json={"title": title}).json()["id"]


TEXT = "# 中序遍历\n\n中序遍历的顺序是先遍历左子树，再访问根节点，最后遍历右子树。普通二叉树不能保证中序结果有序。"


def material(client, sid):
    response = client.post(f"/api/spaces/{sid}/materials", files={"file": ("chapter.md", TEXT.encode(), "text/markdown")})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def seeded_quiz(sid, mid):
    qid, question_id = db.uid(), db.uid()
    questions = [{"id": question_id, "type": "choice", "topic": "中序遍历", "stem": "何时访问根？", "choices": ["最先", "左右子树之间", "最后", "任意"], "answer": 1, "explanation": "中序的根在左右子树之间。", "hint": "想想‘中’的含义。", "evidence": "中序遍历的顺序是先遍历左子树，再访问根节点，最后遍历右子树。"}]
    refs = [{"material_id": mid, "page": 1, "material_title": "chapter", "page_title": "中序遍历"}]
    db.execute("INSERT INTO quizzes VALUES(?,?,?,?,?,?,?,?)", (qid, sid, "中序练习", "", db.dump(questions), db.dump(refs), hashlib.sha256(TEXT.encode()).hexdigest(), db.now()))
    return qid, question_id


def test_answers_hidden_until_submit_and_retries_not_double_counted(client):
    sid = space(client); mid = material(client, sid); qid, q = seeded_quiz(sid, mid)
    path = f"/api/spaces/{sid}/quizzes/{qid}"
    public = client.get(path).json()["questions"][0]
    assert "answer" not in public and "evidence" not in public and not public["hint"]
    hinted = client.post(path + f"/hints/{q}").json()["questions"][0]
    assert hinted["hint"] and "answer" not in hinted
    first = client.post(path + "/answer", json={"question_id": q, "answer": 1}).json()["questions"][0]
    second = client.post(path + "/answer", json={"question_id": q, "answer": 0}).json()["questions"][0]
    assert first["attempt"]["id"] == second["attempt"]["id"]
    assert second["attempt"]["answer"] == "1" and first["attempt"]["hint_used"] == 1
    assert first["answer"] == 1 and first["evidence"]
    p = client.get(f"/api/spaces/{sid}/progress").json()
    assert p["answered"] == 1 and p["correct"] == 1
    assert p["topics"][0]["independent"] == 0
    assert p["topics"][0]["status"] != "已掌握"


def test_source_ids_must_belong_to_current_space(client):
    a, b = space(client), space(client); mid = material(client, a); qid, q = seeded_quiz(a, mid)
    assert client.get(f"/api/spaces/{b}/materials/{mid}/pages/1").status_code == 404
    assert client.post(f"/api/spaces/{b}/chat", json={"material_id": mid, "message": "解释"}).status_code == 404
    assert client.post(f"/api/spaces/{b}/quizzes/{qid}/answer", json={"question_id": q, "answer": 1}).status_code == 404
    assert client.post(f"/api/spaces/{b}/notes", json={"title": "错位笔记", "source": {"material_id": mid}}).status_code == 404
    assert client.get(f"/files/{b}/{mid}").status_code == 404


def test_selection_must_match_and_late_page_passage_is_centered(client):
    sid = space(client); mid = material(client, sid)
    assert client.post(f"/api/spaces/{sid}/chat", json={"material_id": mid,"selection": "不是原文的段落","message": "解释"}).status_code == 422
    from app.vendor.deeptutor_grounding import grounding_context
    context = grounding_context("前" * 15000 + "真正选中的句子" + "后" * 5000, "真正选中的句子")
    assert "真正选中的句子" in context and len(context) <= 6000


def test_settings_never_return_key_and_blank_preserves(client):
    cfg = client.get("/api/settings").json()
    assert "api_key" not in cfg and cfg["has_api_key"]
    assert "sk-unit-test" not in str(cfg)
    response = client.put("/api/settings", json={**cfg,"api_key":""})
    assert response.status_code == 200 and response.json()["has_api_key"]
    assert llm.settings()["api_key"] == "sk-unit-test-private-key"
    assert "sk-unit-test-private-key" not in llm.safe_error(Exception("sk-unit-test-private-key"))


def test_pdf_upload_preserves_physical_pages_and_original_file(client):
    sid = space(client)
    pdf = fitz.open()
    for t in ["Binary tree, page one.", "Inorder: left, root, right."]:
        p = pdf.new_page(); p.insert_text((72, 72), t)
    data = pdf.tobytes(); pdf.close()
    response = client.post(f"/api/spaces/{sid}/materials", files={"file": ("tree.pdf", data, "application/pdf")})
    assert response.status_code == 200
    mat = response.json(); assert mat["page_count"] == 2
    page = client.get(f"/api/spaces/{sid}/materials/{mat['id']}/pages/2").json()
    assert "Inorder" in page["text"]
    assert client.get(f"/files/{sid}/{mat['id']}").content == data
    assert client.put(f"/api/spaces/{sid}/focus", json={"material_id": mat["id"],"page": 3}).status_code == 422


def test_notes_persist_source_and_edits(client):
    sid = space(client); mid = material(client,sid)
    note = client.post(f"/api/spaces/{sid}/notes", json={"title":"中序理解","content":"先左再根后右","source":{"material_id":mid,"page":1}}).json()
    updated = client.put(f"/api/spaces/{sid}/notes/{note['id']}", json={**note,"content":"自己的理解"})
    assert updated.status_code == 200
    rows = client.get(f"/api/spaces/{sid}/notes?q=自己的").json()
    assert len(rows) == 1 and rows[0]["source"]["material_id"] == mid


def test_evidence_check_rejects_fabricated_quote():
    q = {"type":"choice","stem":"问题","choices":["A","B","C","D"],"answer":0,"explanation":"解释","evidence":"这是完全不在原文中的伪造依据句子。"}
    with pytest.raises(ValueError, match="原文对应"):
        learning.validate_questions({"questions":[q]},TEXT)


def test_svg_scripts_external_resources_and_event_handlers_removed():
    result = learning.sanitize_svg('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><foreignObject>bad</foreignObject><rect onclick="bad()" fill="url(https://bad.example)"/><a href="https://bad.example"><text>link</text></a><text>安全</text></svg>')
    assert "script" not in result and "foreignObject" not in result and "onclick" not in result and "bad.example" not in result
    assert "安全" in result
    assert result.startswith("<svg") and "ns0:" not in result


def wait_job(client, sid, jid):
    for _ in range(100):
        result = client.get(f"/api/spaces/{sid}/jobs/{jid}").json()
        if result["status"] in ("failed", "completed"):
            return result
        time.sleep(.02)
    pytest.fail("job did not finish")


def test_async_chat_success_and_error_are_persistent(client, monkeypatch):
    sid = space(client); mid = material(client,sid)
    async def fake(messages, **kwargs):
        assert "中序遍历" in messages[0]["content"]
        if kwargs.get("partial"): kwargs["partial"]("中序先左子树。[来源1]")
        return "中序先左子树。[来源1]"
    monkeypatch.setattr(learning,"complete",fake)
    result=client.post(f"/api/spaces/{sid}/chat",json={"material_id":mid,"message":"解释中序"})
    assert result.status_code==200
    job=wait_job(client,sid,result.json()["job_id"])
    assert job["status"]=="completed",job
    messages=client.get(f"/api/spaces/{sid}/messages").json()
    assert messages[-1]["refs"][0]["material_id"]==mid and messages[-1]["role"]=="assistant"
    async def broken(*args,**kwargs):
        raise ValueError("API 测试错误 sk-unit-test-private-key")
    monkeypatch.setattr(learning,"complete",broken)
    result=client.post(f"/api/spaces/{sid}/chat",json={"material_id":mid,"message":"再次解释"}).json()
    job=wait_job(client,sid,result["job_id"])
    assert job["status"]=="failed" and "sk-unit-test" not in job["error"]
    assert len(client.get(f"/api/spaces/{sid}/messages").json())==3


def test_restart_marks_unfinished_jobs_failed_without_deleting_notes(client):
    sid=space(client)
    client.post(f"/api/spaces/{sid}/notes",json={"title":"持久笔记","content":"保留"})
    jid=db.uid();db.execute("INSERT INTO jobs(id,space_id,kind,status,stage,created,updated) VALUES(?,?,?,?,?,?,?)",(jid,sid,"chat","running","生成中",db.now(),db.now()))
    db.init()
    assert db.one("SELECT status FROM jobs WHERE id=?",(jid,))["status"]=="failed"
    assert client.get(f"/api/spaces/{sid}/notes").json()[0]["content"]=="保留"
