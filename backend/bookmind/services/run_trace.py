"""Safe, presentation-ready projection of persisted ``RunEvent`` records.

``run_events`` is the single append-only trace ledger.  This module does not
write a second trace table: it turns that ledger into a redacted JSON document
or Markdown timeline for debugging and competition presentations.
"""

from __future__ import annotations

from datetime import datetime

from ..domain.models import Run, RunEvent


TRACE_SCHEMA_VERSION = "bookmind.trace.v1"


_STAGE = {
    "run_started": ("生命周期", "开始处理本轮请求"),
    "action_selected": ("路由", "已选择工作流分支"),
    "mode_selected": ("模式", "已切换学习模式"),
    "concept_resolved": ("知识点解析", "已解析本轮涉及的学习单元"),
    "retrieval_scoped": ("检索约束", "已确定资料范围与小节锚点"),
    "tool_started": ("检索", "开始检索教材依据"),
    "source_locations_ready": ("检索", "已定位候选资料位置"),
    "retrieval_completed": ("检索", "教材检索完成"),
    "tool_completed": ("检索", "检索工具完成"),
    "citation_validated": ("引用校验", "教材依据校验完成"),
    "llm_call": ("模型调用", "模型调用遥测已记录"),
    "citation_attached": ("引用", "已附加可回看的资料定位"),
    "answer_completed": ("模型回答", "已生成教材回答"),
    "answer_unavailable": ("模型回答", "本轮未获得可用模型回答"),
    "fallback_used": ("降级", "已采用安全降级路径"),
    "evidence_created": ("学习证据", "已写入学习证据"),
    "state_updated": ("学习状态", "学习状态已更新"),
    "review_scheduled": ("复习计划", "已安排复习"),
    "run_completed": ("生命周期", "本轮处理结束"),
    "run_failed": ("生命周期", "本轮处理失败"),
    "run_cancelled": ("生命周期", "本轮已取消"),
}

# Streaming deltas contain answer prose, and therefore intentionally never
# appear in an exported trace.  The trace records the decision, not the answer.
_OMIT_EVENTS = {"agent_delta", "answer_delta", "agent_started", "agent_completed"}


def build_run_trace(run: Run, events: list[RunEvent], *, include_sensitive: bool = False) -> dict:
    """Project one run into a safe, immutable-trace view."""
    ordered = sorted(events, key=lambda event: event.sequence)
    timeline: list[dict] = []
    previous_at: datetime | None = None
    for event in ordered:
        if event.event_type in _OMIT_EVENTS:
            continue
        stage, summary = _STAGE.get(event.event_type, ("系统", "已记录系统事件"))
        occurred_at = event.created_at
        elapsed = None
        if previous_at is not None:
            elapsed = max(0, round((occurred_at - previous_at).total_seconds() * 1000))
        previous_at = occurred_at
        timeline.append({
            "sequence": event.sequence,
            "event_type": event.event_type,
            "stage": stage,
            "summary": _summary(event.event_type, event.payload, summary),
            "occurred_at": occurred_at.isoformat(),
            "latency_since_previous_ms": elapsed,
            "data": _safe_payload(event.event_type, event.payload, include_sensitive=include_sensitive),
        })

    total_ms = None
    if run.started_at and run.completed_at:
        total_ms = max(0, round((run.completed_at - run.started_at).total_seconds() * 1000))
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "trace_id": f"trace_{run.run_id}",
        "run": {
            "run_id": run.run_id,
            "conversation_id": run.conversation_id,
            "status": run.status,
            "intent": run.intent,
            "model": run.model,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "total_latency_ms": total_ms,
        },
        "summary": {
            "event_count": len(timeline),
            "grounded": _last_bool(ordered, "citation_validated", "grounded"),
            "fallback_used": any(event.event_type == "fallback_used" for event in ordered),
            "state_updated": any(event.event_type == "state_updated" for event in ordered),
        },
        "timeline": timeline,
    }


def render_trace_markdown(trace: dict) -> str:
    """Render a compact Chinese timeline suitable for PPT evidence slides."""
    run = trace["run"]
    summary = trace["summary"]
    lines = [
        "# BookMind 决策 Trace",
        "",
        f"- Trace：`{trace['trace_id']}`",
        f"- Run：`{run['run_id']}`",
        f"- 状态：{run['status']}；意图：{run['intent'] or '未记录'}",
        f"- 教材依据校验：{'通过' if summary['grounded'] else '未通过或未产生'}；"
        f"降级：{'是' if summary['fallback_used'] else '否'}；"
        f"学习状态变化：{'是' if summary['state_updated'] else '否'}",
        "",
        "## 过程时间线",
        "",
    ]
    for index, item in enumerate(trace["timeline"], start=1):
        latency = item.get("latency_since_previous_ms")
        suffix = f"（距上一步 {latency} ms）" if latency is not None else ""
        lines.append(f"{index}. **{item['stage']}**：{item['summary']}{suffix}")
        details = _markdown_details(item["data"])
        if details:
            lines.append(f"   - {details}")
    return "\n".join(lines) + "\n"


def _summary(event_type: str, payload: dict, fallback: str) -> str:
    if event_type == "action_selected":
        return f"意图：{payload.get('intent') or '未识别'}"
    if event_type == "concept_resolved":
        names = "、".join(item.get("name", "") for item in payload.get("concepts", []) if item.get("name"))
        relation = payload.get("followup_relation")
        suffix = (
            "；按上一轮对象继续" if relation == "FOLLOW_UP"
            else "；判定为新话题" if relation == "NEW_TOPIC"
            else "；需要澄清指代" if relation == "AMBIGUOUS"
            else ""
        )
        return f"识别到：{names or '未可靠归类'}{suffix}"
    if event_type == "citation_validated":
        return "教材依据校验通过" if payload.get("grounded") else "教材依据不足或校验未通过"
    if event_type == "llm_call":
        return f"{payload.get('task') or '模型任务'} · {payload.get('model') or '未知模型'}"
    if event_type == "answer_completed":
        return "已生成教材依据回答" if payload.get("grounded") else "已生成但未作为教材结论采用"
    if event_type == "state_updated":
        return "已根据独立作答更新学习状态"
    return fallback


def _safe_payload(event_type: str, payload: dict, *, include_sensitive: bool = False) -> dict:
    """Whitelist export fields; never pass through raw model/user text."""
    payload = payload or {}
    if event_type == "concept_resolved":
        return {
            "query_kind": payload.get("query_kind"),
            "scope": payload.get("scope"),
            "explicit_followup": bool(payload.get("explicit_followup")),
            "followup_relation": payload.get("followup_relation"),
            "followup_confidence": payload.get("followup_confidence"),
            "concepts": [
                {key: item.get(key) for key in ("concept_id", "name", "confidence", "rationale")}
                for item in payload.get("concepts", []) if isinstance(item, dict)
            ],
        }
    if event_type == "retrieval_scoped":
        return {key: payload.get(key) for key in (
            "scope", "source_id", "page", "preferred_chunk_count", "selection_anchor_count",
            "concept_ids", "blocked_by_page_scope",
        ) if key in payload}
    if event_type == "source_locations_ready":
        return {
            "confidence": payload.get("confidence"),
            "grounded": payload.get("grounded"),
            "locations": [
                {key: location.get(key) for key in ("book_id", "page", "page_start", "page_end", "section_path")}
                for location in payload.get("locations", []) if isinstance(location, dict)
            ],
        }
    if event_type in {"retrieval_completed", "citation_validated", "answer_completed", "answer_unavailable"}:
        return {key: payload.get(key) for key in (
            "grounded", "fallback", "citation_count", "reason_code", "model", "live_model", "confidence",
        ) if key in payload}
    if event_type == "citation_attached":
        return {key: payload.get(key) for key in ("index", "chunk_id", "page", "book_id") if key in payload}
    if event_type == "state_updated":
        transitions = payload.get("mastery_transitions", []) + payload.get("misconception_transitions", [])
        return {"transition_count": len(transitions)}
    if event_type == "evidence_created":
        return {"evidence_id": payload.get("evidence_id"), "task_id": payload.get("task_id")}
    if event_type == "action_selected":
        return {key: payload.get(key) for key in ("intent", "workflow") if key in payload}
    if event_type == "llm_call":
        result = {key: payload.get(key) for key in (
            "task", "model", "latency", "latency_ms", "ok", "tokens", "prompt_version", "error_code",
        ) if key in payload}
        if include_sensitive:
            result.update({key: payload.get(key) for key in ("messages", "response") if key in payload})
        return result
    if event_type in {"tool_started", "tool_completed"}:
        return {key: payload.get(key) for key in ("tool", "grounded", "result") if key in payload}
    if event_type in {"fallback_used", "run_failed"}:
        return {"status": "fallback" if event_type == "fallback_used" else "failed"}
    if event_type in {"run_started", "run_completed", "run_cancelled"}:
        return {key: payload.get(key) for key in ("run_status", "status", "message_id") if key in payload}
    return {}


def _last_bool(events: list[RunEvent], event_type: str, key: str) -> bool:
    for event in reversed(events):
        if event.event_type == event_type:
            return bool(event.payload.get(key))
    return False


def _markdown_details(data: dict) -> str:
    concepts = data.get("concepts")
    if concepts:
        return "；".join(
            f"{item.get('name', '未命名')}（{item.get('concept_id', '')}）" for item in concepts
        )
    locations = data.get("locations")
    if locations:
        return "；".join(
            f"第 {item.get('page', '?')} 页 · {' · '.join(item.get('section_path') or [])}"
            for item in locations
        )
    compact = [f"{key}={value}" for key, value in data.items() if value not in (None, "", [], {})]
    return "；".join(compact[:4])


__all__ = ["TRACE_SCHEMA_VERSION", "build_run_trace", "render_trace_markdown"]
