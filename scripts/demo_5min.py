"""5-minute competition demo — drives the full learning closed loop over HTTP
via the product /api/* surface (PRODUCTIZATION §17 demo main line).

This script proves the demo main line runs against the *real* services (not a
hand-wired engine path): it bootstraps a session, creates a project, seeds the
demo textbook, asks a book question with citations, runs the misconception
closure (quiz → SUSPECTED → probe → CONFIRMED → changed task → RESOLVED), and
reads the learning summary — exactly what a judge sees in 5 minutes.

Usage (start the server first, e.g. ``bash start.sh`` in another terminal)::

    python scripts/demo_5min.py                  # http://127.0.0.1:18765
    python scripts/demo_5min.py http://host:port  # custom base URL

Offline by default; set USTC_LLM_API_KEY on the server for live-model enhancement.
Exits non-zero if any step fails, so it doubles as a smoke test.
"""

from __future__ import annotations

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18765"


def _req(path: str, method: str = "GET", body: dict | None = None) -> dict:
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    # Carry the session cookie across requests.
    import http.cookiejar

    if not hasattr(_req, "_jar"):
        _req._jar = http.cookiejar.CookieJar()  # type: ignore[attr-defined]
        _req._opener = urllib.request.build_opener(  # type: ignore[attr-defined]
            urllib.request.HTTPCookieProcessor(_req._jar)  # type: ignore[attr-defined]
        )
    try:
        # A live-model turn (retrieve → embed → rerank → tutor) can take well
        # over 30s on a cold gateway; 90s keeps the smoke test from false-firing
        # on latency alone. Offline turns still return in well under a second.
        with _req._opener.open(req, timeout=90) as r:  # type: ignore[attr-defined]
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:200]}") from None


def step(n: int, title: str) -> None:
    print(f"\n--- {n}. {title} ---")


WRONG = "a.getValue() returns the original value because b is a separate copy."
CORRECT = "a.getValue() returns 9 because a and b refer to the same object."


def main() -> int:
    print("=" * 60)
    print("学迹 / BookMind — 5 分钟演示主线（HTTP /api/*）")
    print(f"服务: {BASE}")
    print("=" * 60)

    step(1, "健康检查 + 模型状态")
    health = _req("/health")
    print(f"  进程: {health.get('status')}")
    models = _req("/health/models")
    print(f"  模型: live={models.get('live')} models={models.get('available_models', [])[:3]}...")

    step(2, "自动登录 + 创建项目 + 载入示例教材")
    me = _req("/api/session/bootstrap", "POST")
    print(f"  匿名用户: {me['user_id']}")
    pid = _req("/api/projects", "POST", {"name": "Java OOP 演示", "goal": "5 分钟演示"})["project_id"]
    seed = _req(f"/api/projects/{pid}/books/seed-demo", "POST")
    print(f"  项目: {pid}  教材知识点: {seed['concepts']}  检索块: {seed['chunks']}")

    step(3, "教材问答（带页码引用）")
    cid = _req(f"/api/projects/{pid}/conversations", "POST")["conversation_id"]
    res = _req(f"/api/conversations/{cid}/messages", "POST", {"content": "引用和对象有什么区别？"})
    conv = _req(f"/api/conversations/{cid}")
    asst = [m for m in conv["messages"] if m["role"] == "assistant"][-1]
    text_blocks = [b for b in asst["content_blocks"] if b["type"] == "text"]
    cites = [b for b in asst["content_blocks"] if b["type"] == "citation"]
    print(f"  回答: {text_blocks[0]['text'][:80] if text_blocks else '(空)'}...")
    print(f"  引用: {len(cites)} 条  示例: {cites[0].get('label') if cites else '无'}")

    step(4, "请求检测 → quiz 答错 → 一次错误不确认误区")
    t1 = _request_task(cid)
    print(f"  任务类型: {t1['kind']}")
    r1 = _req(f"/api/tasks/{t1['task_id']}/answer", "POST", {"answer_text": WRONG, "idempotency_key": "d1"})
    print(f"  判定: {r1['judgment']['result']}  写入证据: {r1['written']}")
    mis = _req(f"/api/projects/{pid}/misconceptions")
    if mis:
        print(f"  误区状态: {mis[0]['status_label']}（一次错误不等于确认）")

    step(5, "探针 → 区分竞争假设 → CONFIRMED → 换情境复验 → RESOLVED")
    # Drive enough probes/changed tasks to reach RESOLVED (offline closure).
    for i in range(5):
        t = _request_task(cid)
        ans = CORRECT if t["kind"] == "changed_task" else WRONG
        r = _req(f"/api/tasks/{t['task_id']}/answer", "POST", {"answer_text": ans, "idempotency_key": f"d{i+2}"})
        mis = _req(f"/api/projects/{pid}/misconceptions")
        status = mis[0]["status_label"] if mis else "—"
        print(f"  第 {i+2} 步: {t['kind']} → {r['judgment']['result']}  误区: {status}")
        if mis and mis[0]["status"] == "RESOLVED":
            print("  ✓ 已纠正（两个不同情境复验通过）")
            break

    step(6, "学习状态 + 恢复建议")
    summary = _req(f"/api/projects/{pid}/learning-summary")
    print(f"  知识点: {summary['total_concepts']}  分组: {summary['groups']}")
    review = _req(f"/api/projects/{pid}/review-plan")
    print(f"  恢复建议: {review['recommendation']}")

    print("\n" + "=" * 60)
    print("演示完成。这条主线展示了普通 RAG 没有的长期学习闭环：")
    print("  教材问答 → 一次错误不确认 → 区分性探针 → 针对性补救 → 换情境复验 → 已纠正")
    print("=" * 60)
    return 0


def _request_task(cid: str) -> dict:
    """Send '考考我' and return the task card from the assistant reply."""
    _req(f"/api/conversations/{cid}/messages", "POST", {"content": "考考我"})
    conv = _req(f"/api/conversations/{cid}")
    asst = [m for m in conv["messages"] if m["role"] == "assistant"][-1]
    for b in asst["content_blocks"]:
        if b.get("type") == "task" and b.get("data", {}).get("kind") in ("quiz", "probe", "changed_task"):
            return b["data"]
    raise RuntimeError("no task card returned")


if __name__ == "__main__":
    sys.exit(main())
