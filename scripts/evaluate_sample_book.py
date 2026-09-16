"""One-book acceptance test for the bundled data-structures textbook.

Runs the real product upload/ingestion path, then asks a curated mix of
definition, algorithm, code, and formula questions through BookQAService.
The report contains no API credential.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from bookmind.api.app import create_app
from bookmind.config import get_settings
from bookmind.services.book_qa import BookQAService
from bookmind.storage.factory import make_repository


CASES = [
    {
        "id": "algorithm-definition",
        "question": "教材如何定义算法？一个算法应具备哪些基本要素？",
        "pages": [(24, 30)],
        "term_groups": [["算法"], ["输入", "输出"]],
    },
    {
        "id": "big-o",
        "question": "大O记号在复杂度分析中表示什么？请结合教材解释它的含义。",
        "pages": [(30, 38)],
        "term_groups": [["大O", "O("], ["上界", "复杂度", "渐进"]],
    },
    {
        "id": "recursion-complexity",
        "question": "教材怎样分析递归算法的时间复杂度？递归跟踪和递推方程分别怎么用？",
        "pages": [(38, 48)],
        "term_groups": [["递归"], ["递推", "方程", "跟踪"]],
    },
    {
        "id": "vector-expansion",
        "question": "向量容量不足时为什么通常采用加倍扩容？它的分摊时间复杂度是多少？",
        "pages": [(55, 66)],
        "term_groups": [["扩容", "容量"], ["分摊", "均摊", "O(1)"]],
    },
    {
        "id": "stack-parentheses",
        "question": "如何利用栈检查表达式中的括号是否匹配？说明算法思路和复杂度。",
        "pages": [(108, 121)],
        "term_groups": [["栈"], ["括号"], ["O(n)", "线性"]],
    },
    {
        "id": "tree-traversal",
        "question": "二叉树的先序、中序和后序遍历有什么区别？教材给出的迭代实现依赖什么结构？",
        "pages": [(145, 158)],
        "term_groups": [["先序"], ["中序"], ["后序"], ["栈"]],
    },
    {
        "id": "huffman",
        "question": "Huffman编码树如何构造，为什么它能得到最优前缀编码？",
        "pages": [(158, 171)],
        "term_groups": [["Huffman", "哈夫曼"], ["前缀", "编码"]],
    },
    {
        "id": "bfs-dfs",
        "question": "图的广度优先搜索和深度优先搜索分别使用什么辅助结构，主要区别是什么？",
        "pages": [(181, 190)],
        "term_groups": [["广度", "BFS"], ["深度", "DFS"], ["队列", "栈"]],
    },
    {
        "id": "avl-rotation",
        "question": "AVL树失衡后为什么可以通过旋转恢复平衡？LL、RR、LR、RL四种情形如何处理？",
        "pages": [(213, 225)],
        "term_groups": [["AVL"], ["旋转"], ["LL", "RR", "LR", "RL"]],
    },
    {
        "id": "b-tree",
        "question": "B-树为什么适合外部存储？查找、插入发生上溢时如何处理？",
        "pages": [(234, 249)],
        "term_groups": [["B-树", "B树"], ["上溢", "分裂"], ["I/O", "外存", "磁盘"]],
    },
    {
        "id": "hash-collision",
        "question": "散列表发生冲突时，教材介绍了哪些解决办法？开放定址和独立链各有什么特点？",
        "pages": [(281, 303)],
        "term_groups": [["冲突"], ["开放定址"], ["独立链", "链地址"]],
    },
    {
        "id": "heap-filter",
        "question": "二叉堆的上滤和下滤分别在什么情况下发生？二者时间复杂度是多少？",
        "pages": [(303, 327)],
        "term_groups": [["上滤"], ["下滤"], ["O(log", "对数"]],
    },
    {
        "id": "kmp",
        "question": "KMP字符串匹配算法如何利用next表避免回退文本指针？",
        "pages": [(327, 355)],
        "term_groups": [["KMP"], ["next"], ["回退", "匹配"]],
    },
    {
        "id": "bitmap-clear",
        "question": "习题中的O(1)初始化Bitmap初版为什么不支持clear操作，改进版如何解决？",
        "pages": [(485, 488)],
        "term_groups": [["set"], ["clear"], ["T[", "校验环", "取负"]],
    },
    {
        "id": "d-ary-heap-formula",
        "question": "教材中d叉堆的高度、上滤和下滤复杂度分别是什么？为什么下滤还要乘以d？",
        "pages": [(623, 627)],
        "term_groups": [["d叉堆", "d 叉堆"], ["log"], ["O(d", "乘以d", "d个孩子"]],
    },
]


def _matches_groups(text: str, groups: list[list[str]]) -> bool:
    folded = text.casefold().replace(" ", "")
    return all(any(term.casefold().replace(" ", "") in folded for term in group) for group in groups)


def _citation_in_range(citations: list[dict], ranges: list[tuple[int, int]]) -> bool:
    for citation in citations:
        start = int(citation.get("page_start") or citation.get("page") or 0)
        end = int(citation.get("page_end") or start)
        if any(start <= upper and end >= lower for lower, upper in ranges):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=int, default=len(CASES))
    parser.add_argument("--timeout", type=int, default=2400)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.sample_book_file.is_file():
        raise SystemExit(f"sample PDF not found: {settings.sample_book_file}")
    if not settings.llm_api_key():
        raise SystemExit("DeepSeek API key was not loaded")

    repo = make_repository(settings)
    app = create_app(repo=repo)
    started = time.monotonic()
    with TestClient(app) as client:
        user = client.post("/api/session/bootstrap").json()
        project_response = client.post(
            "/api/projects",
            json={"name": "数据结构单书验收", "goal": "验证教材处理、公式和问答"},
        )
        project_response.raise_for_status()
        project_id = project_response.json()["project_id"]

        import_response = client.post(f"/api/projects/{project_id}/sources/sample")
        import_response.raise_for_status()
        imported = import_response.json()
        book_id, job_id = imported["book_id"], imported["job_id"]
        print(json.dumps({
            "event": "ingestion_started", "project_id": project_id,
            "book_id": book_id, "job_id": job_id,
        }, ensure_ascii=False), flush=True)

        last_marker = None
        while True:
            status_response = client.get(f"/api/jobs/{job_id}")
            status_response.raise_for_status()
            status = status_response.json()
            marker = (
                status["state"], status["stage"],
                int(status.get("pages_done") or 0) // 20,
            )
            if marker != last_marker:
                print(json.dumps({
                    "event": "ingestion_progress", "state": status["state"],
                    "stage": status["stage"], "progress": status["progress"],
                    "pages_done": status.get("pages_done", 0),
                    "pages_total": status.get("pages_total", 0),
                }, ensure_ascii=False), flush=True)
                last_marker = marker
            if status["state"] in {"SUCCEEDED", "FAILED", "RETRYABLE_FAILED", "CANCELLED"}:
                break
            if time.monotonic() - started > args.timeout:
                raise SystemExit("ingestion timed out")
            time.sleep(2)

        if status["state"] != "SUCCEEDED":
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 2

        qa = BookQAService(repo, app.state.model_router)
        results = []
        for index, case in enumerate(CASES[: max(1, args.questions)], start=1):
            t0 = time.monotonic()
            answer = qa.ask(
                project_id=project_id,
                learner_id=user["user_id"],
                question=case["question"],
                source_ids=[book_id],
                top_k=12,
                context_budget=12,
            )
            latency = round(time.monotonic() - t0, 2)
            evidence_text = answer.answer_text + "\n" + "\n".join(
                str(item.get("quote") or "") for item in answer.citations
            )
            keyword_ok = _matches_groups(evidence_text, case["term_groups"])
            page_ok = _citation_in_range(answer.citations, case["pages"])
            citation_ok = bool(answer.citations) and all(
                item.get("chunk_id") and item.get("quote") for item in answer.citations
            )
            insufficiency_markers = (
                "依据不足", "无法回答", "无法完整", "没有给出", "未说明",
                "没有明确", "未明确",
            )
            answer_sufficient = not any(
                marker in answer.answer_text for marker in insufficiency_markers
            )
            passed = bool(
                answer.grounded and citation_ok and keyword_ok and page_ok
                and answer_sufficient
            )
            row = {
                **case,
                "answer": answer.answer_text,
                "citations": answer.citations,
                "grounded": answer.grounded,
                "fallback": answer.fallback,
                "reason": answer.reason,
                "retrieval_confidence": answer.retrieval_confidence,
                "keyword_ok": keyword_ok,
                "page_ok": page_ok,
                "citation_ok": citation_ok,
                "answer_sufficient": answer_sufficient,
                "passed": passed,
                "latency_seconds": latency,
            }
            results.append(row)
            print(json.dumps({
                "event": "question_completed", "number": index,
                "id": case["id"], "passed": passed,
                "grounded": answer.grounded, "page_ok": page_ok,
                "latency_seconds": latency,
            }, ensure_ascii=False), flush=True)

        passed_count = sum(bool(row["passed"]) for row in results)
        grounded_count = sum(bool(row["grounded"]) for row in results)
        report = {
            "created_at": datetime.now().astimezone().isoformat(),
            "source_file": settings.sample_book_file.name,
            "model_provider": "DeepSeek official API",
            "chat_model": settings.chat_model or "deepseek-flash",
            "retrieval": "BM25 + DeepSeek query expansion + DeepSeek chat rerank",
            "project_id": project_id,
            "book_id": book_id,
            "job": status,
            "summary": {
                "questions": len(results), "passed": passed_count,
                "grounded": grounded_count,
                "pass_rate": round(passed_count / max(1, len(results)), 4),
                "grounded_rate": round(grounded_count / max(1, len(results)), 4),
                "elapsed_seconds": round(time.monotonic() - started, 2),
            },
            "results": results,
        }
        output_dir = Path(settings.data_dir) / "evaluations"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"dsacpp_deepseek_{datetime.now():%Y%m%d_%H%M%S}.json"
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "event": "evaluation_completed", **report["summary"],
            "report": str(output_path.resolve()),
        }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
