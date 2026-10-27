"""Opt-in verification against the locally configured paid model API.

Uses only the local backend; never reads or prints an API key. Creates two
clearly identified demonstration learning spaces and saves an audit report.
"""
import json
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
report = {"verified_at": time.strftime("%Y-%m-%d %H:%M:%S"), "checks": []}


def record(name, **extra):
    report["checks"].append({"name": name, "ok": True, **extra})
    (ROOT / "data" / "live-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(name + ": PASS", flush=True)


with httpx.Client(base_url="http://127.0.0.1:8018", timeout=240, trust_env=False) as client:
    def get(path):
        response = client.get("/api" + path); response.raise_for_status(); return response.json()

    def post(path, body):
        response = client.post("/api" + path, json=body); response.raise_for_status(); return response.json()

    def job(sid, kind, body):
        jid = post(f"/spaces/{sid}/{kind}", body)["job_id"]
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            state = get(f"/spaces/{sid}/jobs/{jid}")
            if state["status"] == "failed":
                raise RuntimeError(state["error"])
            if state["status"] == "completed":
                return state["result"]
            time.sleep(1)
        raise RuntimeError("Live generation timed out")

    cfg = get("/settings")
    assert "api_key" not in cfg and cfg["has_api_key"]
    connection = post("/settings/test", cfg)
    assert connection["ok"]
    record("actual_model_connection", model=connection["model"])

    sid = post("/spaces", {"title": "数据结构 · 二叉树", "goal": "理解二叉树结构和三种遍历"})["id"]
    report["reading_space_id"] = sid
    text = (ROOT / "frontend" / "public" / "demo.md").read_bytes()
    response = client.post(f"/api/spaces/{sid}/materials", files={"file": ("二叉树学习资料.md", text, "text/markdown")})
    response.raise_for_status(); mat = response.json(); mid = mat["id"]
    # Demo Markdown has an introduction and three sections. Pick traversal section.
    page = next(o["page"] for o in mat["outline"] if "三种" in o["title"])
    client.put(f"/api/spaces/{sid}/focus", json={"material_id":mid,"page":page}).raise_for_status()
    body = {"material_id":mid,"page":page,"scope":"page"}
    record("upload_and_read", material_id=mid, sections=mat["page_count"], page=page)

    job(sid,"chat",{**body,"message":"我总是把中序遍历和前序遍历搞混。请先用最简单的话解释差别，再用资料里的树举例，不超过500字。"})
    messages = get(f"/spaces/{sid}/messages")
    assert messages[-1]["role"] == "assistant" and messages[-1]["refs"][0]["material_id"] == mid
    assert len(messages[-1]["content"]) > 80
    record("grounded_chat", characters=len(messages[-1]["content"]))

    result = job(sid,"visualize",{**body,"message":"请用可逐步播放的steps格式，演示示例二叉树 A(B(D,E),C) 的中序遍历。图中保留五个节点，逐步显示递归调用、访问时机和输出序列，最终为 D B E A C。"})
    art = get(f"/spaces/{sid}/artifacts/{result['artifact_id']}")
    assert art["format"] == "steps" and len(art["payload"]["steps"]) >= 4
    report["artifact_id"] = art["id"]
    record("interactive_visualization", format=art["format"], steps=len(art["payload"]["steps"]))
    note=post(f"/spaces/{sid}/notes",{"title":"中序遍历：根为什么在中间？","content":"中序遍历的关键：先把左子树处理完，再访问根，然后处理右子树。\n\n示例树的输出是 **D → B → E → A → C**。\n\n我容易混淆的地方：递归进入节点不代表立刻输出节点；输出时机由遍历规则决定。", "source":{**art["refs"][0],"artifact_id":art["id"]}})
    assert get(f"/spaces/{sid}/notes")[0]["source"]["artifact_id"] == art["id"]
    record("note_with_diagram_and_source", note_id=note["id"])

    result=job(sid,"quizzes",{**body,"topic":"中序遍历与前序遍历的区别","count":3})
    quiz=get(f"/spaces/{sid}/quizzes/{result['quiz_id']}")
    assert len(quiz["questions"]) == 3 and all("answer" not in q for q in quiz["questions"])
    report["quiz_id"]=quiz["id"]
    # Deliberately use a hint on the first question, leave the rest for UI operation.
    q=quiz["questions"][0]
    post(f"/spaces/{sid}/quizzes/{quiz['id']}/hints/{q['id']}",{})
    graded=post(f"/spaces/{sid}/quizzes/{quiz['id']}/answer",{"question_id":q["id"],"answer":0})
    assert graded["questions"][0]["attempt"]["hint_used"] == 1
    assert get(f"/spaces/{sid}/progress")["answered"] == 1
    record("quiz_generation_hidden_answers_and_grading", questions=len(quiz["questions"]))

    sid2=post("/spaces",{"title":"从零理解递归", "goal":"我是大学一年级学生，想从零理解 Python 递归，包括递归终止条件、调用栈和二叉树遍历。"})["id"]
    report["goal_space_id"]=sid2
    result=job(sid2,"handbook",{"goal":"我是大学一年级学生，想从零理解 Python 递归，包括递归终止条件、调用栈和二叉树遍历。请编写4节短学习手册。"})
    generated=get(f"/spaces/{sid2}")["materials"][0]
    assert generated["kind"]=="generated" and generated["page_count"]>=2
    record("goal_to_learning_handbook", chapters=generated["page_count"], material_id=result["material_id"])

print("Live workflow complete. See data/live-verification.json.",flush=True)
