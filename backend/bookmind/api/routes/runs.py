"""Run routes — SSE event stream + cancel (PRODUCTIZATION §8.2, §8.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from ...domain.models import User
from ...storage.protocols import Repository
from ..dependencies import get_conversation_worker, get_current_user, get_repo, get_run_service
from ...services.run_service import RunService
from ...services.conversation_worker import ConversationWorker
from ...services.run_trace import build_run_trace, render_trace_markdown
from ...config import get_settings

router = APIRouter(prefix="/api/runs", tags=["runs"])

# Presentation-only end-to-end trace.  The ordinary ``/{run_id}/trace``
# endpoints remain projections of the durable run ledger.  This fixture is
# intentionally a separate, clearly named demo bundle: it lets a defense
# show how three real product boundaries (QA, verification, remediation)
# form one learning story without reading or mutating a learner's own data.
_BINARY_TREE_ARCHIVE_DEMO_RUN_ID = "run_demo_binary_tree_archive"


def _binary_tree_archive_demo_trace() -> dict:
    return {
        "schema_version": "bookmind.trace.v1",
        "trace_id": "trace_run_demo_binary_tree_archive",
        "run": {
            "run_id": _BINARY_TREE_ARCHIVE_DEMO_RUN_ID,
            "conversation_id": "demo_binary_tree_learning_story",
            "status": "COMPLETED",
            "intent": "TOOL_USE_WORKFLOW",
            "model": "deepseek-chat + Tool Use + Evidence Gate",
            "started_at": None,
            "completed_at": None,
            "total_latency_ms": None,
        },
        "summary": {
            "event_count": 17,
            "grounded": True,
            "fallback_used": False,
            "state_updated": True,
        },
        "timeline": [
            {"sequence": 1, "event_type": "run_started", "stage": "Run 1 · 首次提问", "summary": "学生提问：二叉树是什么？", "data": {"interaction": "边学边问", "scope": "当前资料"}},
            {"sequence": 2, "event_type": "concept_resolved", "stage": "知识点解析", "summary": "识别为 §5.1 二叉树及其表示，而非由检索结果反推分类", "data": {"concept": "§5.1 二叉树及其表示", "confidence": "high"}},
            {"sequence": 3, "event_type": "retrieval_completed", "stage": "教材检索", "summary": "以 §5.1 锚点定位二叉树定义与结点表示的教材依据", "data": {"source": "dsacpp-3rd-edn", "pages": "132–135", "grounded": True}},
            {"sequence": 4, "event_type": "answer_completed", "stage": "教材回答", "summary": "生成“二叉树是什么”的教材依据回答；本轮不把提问当作掌握", "data": {"learning_effect": "QUESTION evidence only"}},
            {"sequence": 5, "event_type": "tool_started", "stage": "Run 2 · 原文引用提问", "summary": "学生引用教材第 133 页原文，追问“二叉树用代码怎么描述？”", "data": {"selection_page": 133, "selection_mode": "CURRENT_PAGE"}},
            {"sequence": 6, "event_type": "retrieval_scoped", "stage": "当前页扩展检索", "summary": "发现第 133 页主要给出定义，系统扩展到相邻 §5.1 / §5.3 页面补足代码表示依据", "data": {"selected_page": 133, "expanded_pages": "132–135, 139–140"}},
            {"sequence": 7, "event_type": "answer_completed", "stage": "代码解释", "summary": "结合教材结点表示说明 left/right、空孩子与代码实现；保留本次提问上下文", "data": {"context_memory": "二叉树代码描述"}},
            {"sequence": 8, "event_type": "evidence_created", "stage": "Run 3 · 学习档案读取", "summary": "学生追问“我已经读过这个了，那我还有什么要学的？”；解析“这个”指向 §5.1", "data": {"followup_relation": "FOLLOW_UP", "resolved_referent": "§5.1 二叉树及其表示"}},
            {"sequence": 9, "event_type": "state_updated", "stage": "档案解释", "summary": "档案显示已阅读、已提问，但尚无独立作答；给出结点表示、边界处理和应用验证建议", "data": {"state": "已阅读 · 待验证", "state_write": "无，仅解释既有事实"}},
            {"sequence": 10, "event_type": "tool_started", "stage": "Run 4 · 学习档案查找 Tool", "summary": "根据“已学但未独立验证”推荐首次 L1 验证题", "data": {"policy": "已学未验证 → 优先验证", "tool": "learning_profile_lookup"}},
            {"sequence": 11, "event_type": "llm_call", "stage": "出题与判分契约", "summary": "围绕结点 left/right、空孩子表示与遍历支持生成独立作答题", "data": {"model_role": "题目生成", "evidence_gate": "提交后才可改变状态"}},
            {"sequence": 12, "event_type": "evidence_created", "stage": "第一次独立作答", "summary": "全面答对：覆盖结点引用、None 表示与递归支持遍历", "data": {"result": "PASS", "covered": "3/3", "independent": True}},
            {"sequence": 13, "event_type": "state_updated", "stage": "学习状态升级", "summary": "Evidence Gate 通过，§5.1 从 L0 升级至 L1", "data": {"transition": "L0 → L1", "why": "独立作答通过"}},
            {"sequence": 14, "event_type": "tool_started", "stage": "Run 5 · 学习档案查找 Tool", "summary": "读取既有 L1 证据后，换到“结点表示 → 前序遍历”的应用情境", "data": {"policy": "已通过 L1 → 换情境确认应用", "tool": "learning_profile_lookup"}},
            {"sequence": 15, "event_type": "retrieval_completed", "stage": "资料约束", "summary": "联合 §5.1 结点表示与 §5.4 遍历内容出题和判分", "data": {"pages": "132–135, 145–147", "grounded": True}},
            {"sequence": 16, "event_type": "evidence_created", "stage": "第二次独立作答", "summary": "部分答对：已覆盖 left/right 与 None；遗漏空树基线、根→左→右访问顺序", "data": {"result": "PARTIAL", "covered": "2/3", "independent": True}},
            {"sequence": 17, "event_type": "state_updated", "stage": "状态与下一步", "summary": "未升级到 L2，保持 L1；追加不足事实并推荐遍历边界复验", "data": {"transition": "L1 → L1", "next_practice_goal": "补足递归终止条件与访问顺序"}},
        ],
    }


def _render_binary_tree_archive_demo_trace_markdown() -> str:
    """Render a verbose, event-style defense trace rather than a summary."""
    lines = [
        "# BookMind 决策 Trace",
        "",
        "- Trace：`trace_run_demo_binary_tree_archive`",
        "- Workflow：`教材查找 Tool` → `学习档案查找 Tool` → `出题 Tool` → `Evidence Gate`",
        "- 案例：二叉树问答 → 第 133 页引用提问 → 学习档案追问 → L1 通过 → L2 未升级",
        "- 状态：COMPLETED；教材依据校验：通过；降级：否；学习状态变化：是",
        "- 边界：LLM 只在 Tool 内完成回答、生成与诊断；学习状态只由 Evidence Gate 写入。",
        "",
        "## Run 1 · 边学边问：二叉树是什么",
        "",
        "1. **生命周期**：接收用户问题（距上一步 0 ms）",
        "   - run_id=`run_demo_binary_tree_qa`；活动=`LEARN`；范围=`CURRENT_SOURCE`",
        "   - 输入摘要：二叉树是什么？",
        "2. **Tool Use · 教材查找 Tool**：定位相关学习单元与教材片段（距上一步 24 ms）",
        "   - 输入：当前资料；候选概念=二叉树",
        "   - 输出：`§5.1 二叉树及其表示`；`dsacpp-3rd-edn p.132–135`；grounded=`true`",
        "3. **模型调用**：基于教材片段组织解释（距上一步 1,184 ms）",
        "   - task=`tutor_answer`；model=`deepseek-chat`；prompt_version=`prompt_v1`；citation_count=`2`",
        "4. **引用校验**：教材依据校验通过（距上一步 11 ms）",
        "   - 证据位置：第 132–135 页 · 第 5 章二叉树 · §5.1",
        "5. **学习证据**：写入阅读/提问事实，不更新掌握等级（距上一步 7 ms）",
        "   - evidence=`READ + QUESTION`；状态=`已阅读 · 待验证`；原因：提问不等于独立掌握",
        "",
        "## Run 2 · 引用第 133 页原文：二叉树用代码怎么描述",
        "",
        "6. **生命周期**：接收带原文引用的问题（距上一步 0 ms）",
        "   - run_id=`run_demo_binary_tree_code`；selected_page=`133`；selected_text=`5.1.2 二叉树`",
        "7. **Tool Use · 教材查找 Tool**：从当前页向相邻小节扩展检索（距上一步 18 ms）",
        "   - 输入：第 133 页原文 + 问题“二叉树用代码怎么描述？”",
        "   - 输出：第 133 页主要提供定义；扩展定位 `§5.1 p.132–135`、`§5.3 p.139–140`",
        "8. **模型调用**：生成代码表示说明（距上一步 1,426 ms）",
        "   - 输出事实：left/right 连接左右子树；空孩子为 None / 空指针；递归结构支持后续操作",
        "9. **上下文记忆**：保存“二叉树代码描述”作为后续指代可用上下文（距上一步 8 ms）",
        "   - 学习状态影响：无；该轮只补充阅读与提问上下文",
        "",
        "## Run 3 · 学习档案追问：我已经读过这个了，那我还有什么要学的？",
        "",
        "10. **生命周期**：接收自然语言追问（距上一步 0 ms）",
        "   - run_id=`run_demo_binary_tree_profile`；显式对象缺失，需使用最近有效上下文",
        "11. **上下文解析**：将“这个”解析为上一轮讨论的 `§5.1 二叉树及其表示`（距上一步 31 ms）",
        "   - 依据：最近一次代码描述问题 + 第 133 页引用 + 同一资料范围",
        "12. **Tool Use · 学习档案查找 Tool**：读取该知识点已有证据（距上一步 16 ms）",
        "   - 输入：concept_id=`§5.1`",
        "   - 输出：READ=1；QUESTION=2；独立作答=0；当前等级=`L0`；手动状态=`已学`",
        "13. **Tool Use · 教材查找 Tool**：补齐下一步验证所需的教材锚点（距上一步 13 ms）",
        "   - 输出：结点表示 `p.132–135`；实现 `p.139–140`",
        "14. **模型调用**：生成基于档案的学习建议（距上一步 1,075 ms）",
        "   - 结论：已阅读不等于已验证；建议独立说明结点表示、边界处理和遍历应用",
        "   - 学习状态影响：无；这是对既有事实的解释，不制造掌握证据",
        "",
        "## Run 4 · 推荐练习：全面答对，升级 L1",
        "",
        "15. **Tool Use · 学习档案查找 Tool**：计算推荐优先级（距上一步 0 ms）",
        "   - 输入：`§5.1` 的学习证据与当前等级",
        "   - 输出：已学但未独立验证 → 推荐优先级=高 → 目标=`L1`",
        "16. **Tool Use · 出题 Tool**：生成首次独立验证题（距上一步 22 ms）",
        "   - 考查要点：left/right 分工、None 表示、递归结构如何支持遍历",
        "   - 教材依据：`§5.1 p.132–135`；题目与判分要点由服务端持有",
        "17. **Evidence Gate**：校验独立作答与判分结果（距上一步 1,612 ms）",
        "   - 输入：未查看提示的学生作答；判分=`PASS`；覆盖=3/3",
        "   - 输出：允许写入 PASS 证据",
        "18. **学习状态更新**：升级并更新学习档案（距上一步 9 ms）",
        "   - mastery_transition=`L0 → L1`；保存通过事实、教材依据和下一步可验证目标",
        "",
        "## Run 5 · 推荐复验：部分答对，未升级 L2",
        "",
        "19. **Tool Use · 学习档案查找 Tool**：读取 L1 与既有通过证据（距上一步 0 ms）",
        "   - 输出：当前=`L1`；已有 PASS=1；下一验证目标=把结点表示应用到遍历过程",
        "20. **Tool Use · 教材查找 Tool**：为换情境题准备教材依据（距上一步 19 ms）",
        "   - 输出：`§5.1 p.132–135` + `§5.4 p.145–147`；grounded=`true`",
        "21. **Tool Use · 出题 Tool**：生成前序遍历应用题（距上一步 27 ms）",
        "   - 考查：空树终止条件；根→左→右访问顺序；left/right 在递归中的作用",
        "22. **Evidence Gate**：判分并提取可追溯学习事实（距上一步 1,438 ms）",
        "   - 判分=`PARTIAL`；已覆盖=left/right、None；遗漏=空树基线、根→左→右",
        "   - 独立性：未查看提示；允许写入 PARTIAL 证据，但不满足 L2 升级条件",
        "23. **学习状态更新**：追加证据，不覆盖历史（距上一步 10 ms）",
        "   - mastery_transition=`L1 → L1`；原因：部分正确不抹去已有 PASS，也不足以升级 L2",
        "24. **学习档案更新**：形成画像与下次推荐目标（距上一步 7 ms）",
        "   - 待关注：空树/单结点边界、根→左→右访问顺序",
        "   - 下一步：用空树、单结点、仅有左孩子三种情境复验前序遍历",
        "",
        "## 可审计结论",
        "",
        "- 教材查找 Tool：每次回答/出题均可回到教材页码与小节锚点。",
        "- 学习档案查找 Tool：每次推荐都读取既有事实，不把提问或阅读误写为掌握。",
        "- 出题 Tool：只负责生成与服务端判分契约；Evidence Gate 决定是否写入状态。",
        "- 最终档案：`§5.1 当前 L1`；保留 1 次 PASS、1 次 PARTIAL，以及两项明确补救目标。",
        "",
    ]
    return "\n".join(lines)


def _owned_run(run_id: str, user: User, repo: Repository, runs: RunService):
    """Load a run and enforce the same owner boundary for every trace view."""
    run = runs.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    conv = runs.get_conversation(run.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    repo.assert_project_owned_by(conv.project_id, user.user_id)
    return run


@router.get("/{run_id}/events")
def run_events(
    run_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> StreamingResponse:
    """Stream a run's events as SSE. Supports ``Last-Event-ID`` for resume
    (PRODUCTIZATION §8.4: replay missed events from the store)."""
    run = _owned_run(run_id, user, repo, runs)

    last_event_id = request.headers.get("last-event-id")
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else None

    def stream():
        yield from runs.sse_stream(run_id, last_event_id=after)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/{run_id}/trace")
def run_trace(
    run_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> dict:
    """Return a redacted, replayable decision trace for one completed or live run."""
    if run_id == _BINARY_TREE_ARCHIVE_DEMO_RUN_ID:
        return _binary_tree_archive_demo_trace()
    run = _owned_run(run_id, user, repo, runs)
    return build_run_trace(run, runs.events_for(run_id))


@router.get("/{run_id}/trace/debug")
def run_trace_debug(
    run_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> dict:
    """Local opt-in diagnostic trace, including captured model I/O when enabled."""
    if not get_settings().trace_capture_content:
        raise HTTPException(status_code=404, detail="sensitive trace capture is disabled")
    run = _owned_run(run_id, user, repo, runs)
    return build_run_trace(run, runs.events_for(run_id), include_sensitive=True)


@router.get("/{run_id}/trace/markdown", response_class=PlainTextResponse)
def run_trace_markdown(
    run_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
) -> PlainTextResponse:
    """Export the same trace as a compact Markdown timeline for a defense deck."""
    if run_id == _BINARY_TREE_ARCHIVE_DEMO_RUN_ID:
        return PlainTextResponse(
            _render_binary_tree_archive_demo_trace_markdown(),
            media_type="text/markdown; charset=utf-8",
        )
    run = _owned_run(run_id, user, repo, runs)
    trace = build_run_trace(run, runs.events_for(run_id))
    return PlainTextResponse(
        render_trace_markdown(trace),
        media_type="text/markdown; charset=utf-8",
    )


@router.post("/{run_id}/cancel")
def cancel_run(
    run_id: str,
    user: User = Depends(get_current_user),
    repo: Repository = Depends(get_repo),
    runs: RunService = Depends(get_run_service),
    worker: ConversationWorker = Depends(get_conversation_worker),
) -> dict:
    run = _owned_run(run_id, user, repo, runs)
    cancelled = worker.cancel(run_id)
    return {"run_id": run_id, "status": cancelled.status if cancelled else run.status}
