from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from bookmind.domain.models import Run, RunEvent
from bookmind.services.run_trace import TRACE_SCHEMA_VERSION, build_run_trace, render_trace_markdown


def _event(sequence: int, event_type: str, payload: dict, at: datetime) -> RunEvent:
    return RunEvent(
        run_id="run_trace_demo", sequence=sequence, event_type=event_type,
        payload=payload, created_at=at,
    )


def test_trace_is_ordered_redacted_and_presentation_ready():
    started = datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)
    run = Run(
        run_id="run_trace_demo", conversation_id="conv_demo", message_id="msg_demo",
        status="COMPLETED", intent="ASK_BOOK", model="deepseek-chat",
        started_at=started, completed_at=started + timedelta(seconds=3),
    )
    events = [
        _event(3, "agent_delta", {"text": "这段完整模型回答不应导出"}, started + timedelta(seconds=2)),
        _event(1, "action_selected", {"intent": "ASK_BOOK", "workflow": "langgraph"}, started),
        _event(2, "concept_resolved", {
            "query_kind": "definition",
            "concepts": [{"concept_id": "sec_stack", "name": "栈", "confidence": 0.96, "rationale": "术语命中"}],
            "scope": "ALL_SOURCES", "explicit_followup": True,
            "followup_relation": "FOLLOW_UP", "followup_confidence": 0.96,
            "raw_user_question": "栈是什么",  # must not leak even if an upstream event is malformed
        }, started + timedelta(seconds=1)),
        _event(4, "citation_validated", {
            "grounded": True, "citation_count": 2, "fallback": False, "reason_code": "OK",
            "rubric": "也不应导出",
        }, started + timedelta(seconds=2, milliseconds=100)),
        _event(5, "state_updated", {
            "mastery_transitions": [{"concept_id": "sec_stack", "secret": "never export details"}],
        }, started + timedelta(seconds=3)),
    ]

    trace = build_run_trace(run, events)
    serialized = json.dumps(trace, ensure_ascii=False)

    assert trace["schema_version"] == TRACE_SCHEMA_VERSION
    assert trace["summary"] == {"event_count": 4, "grounded": True, "fallback_used": False, "state_updated": True}
    assert [item["sequence"] for item in trace["timeline"]] == [1, 2, 4, 5]
    assert "完整模型回答" not in serialized
    assert "栈是什么" not in serialized
    assert "不应导出" not in serialized
    assert trace["timeline"][-1]["data"] == {"transition_count": 1}
    resolved = trace["timeline"][1]
    assert resolved["data"]["followup_relation"] == "FOLLOW_UP"
    assert "按上一轮对象继续" in resolved["summary"]

    markdown = render_trace_markdown(trace)
    assert "# BookMind 决策 Trace" in markdown
    assert "知识点解析" in markdown
    assert "栈（sec_stack）" in markdown
    assert "完整模型回答" not in markdown


def test_llm_telemetry_is_visible_but_sensitive_io_requires_explicit_export():
    now = datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc)
    run = Run(run_id="run_trace_demo", conversation_id="conv", message_id="msg", status="COMPLETED")
    events = [_event(1, "llm_call", {
        "task": "tutor_answer", "model": "deepseek-chat", "latency_ms": 431,
        "tokens": {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165},
        "ok": True, "messages": [{"role": "user", "content": "private prompt"}],
        "response": "private answer",
    }, now)]
    safe = build_run_trace(run, events)
    debug = build_run_trace(run, events, include_sensitive=True)
    assert safe["timeline"][0]["data"]["tokens"]["total_tokens"] == 165
    assert "messages" not in safe["timeline"][0]["data"]
    assert debug["timeline"][0]["data"]["response"] == "private answer"
