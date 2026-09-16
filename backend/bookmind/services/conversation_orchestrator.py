"""Conversation Orchestrator — the user-facing turn coordinator
(PRODUCTIZATION §6, M2).

This is an application service, NOT a fourth decision-making Agent. It only
*orchestrates*: it identifies intent, routes to the right existing service
(BookQAService, Diagnostician, Learning Engine), persists messages/runs/events,
and emits a unified event stream for the frontend. The deterministic learning
rules remain the sole authority of the Engine — the Orchestrator never decides
mastery, misconception scores, or Next Best Action on its own.

Atomic turn boundary (§6.3):
  1. write user message
  2. create run + idempotency key
  3. emit queued
  4. execute retrieval / generation / validation / diagnosis
  5. if state change needed, write Evidence in the Engine transaction
  6. write assistant message + trace
  7. mark run completed
  8. emit run_completed

Intent (§6.2): a small, explainable set. Deterministic priority rule first
(pending task → treat as answer; else → textbook Q&A); LLM intent classification
when available, falling back to the deterministic rule on any failure. A model
failure must never lose the user's message.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import Callable, Iterator, Literal, TypedDict

from ..domain.enums import (
    Action,
    EvidenceType,
    EventType,
    UIPreset,
)
from ..domain.models import (
    ContentBlock,
    Conversation,
    Message,
    Run,
    RunEvent,
)
from ..engine.action_matrix import dimensions_for
from ..engine.decision.next_action import ConceptView, DecisionInput, decide
from ..engine.learning_engine import record_exposure
from ..llm.router import ModelRouter
from ..services.book_qa import BookQAService
from ..storage.protocols import Repository


# --- intent ---------------------------------------------------------------

Intent = Literal[
    "ASK_BOOK", "START_LEARNING", "REQUEST_EXPLANATION", "REQUEST_TASK",
    "SUBMIT_ANSWER", "REQUEST_HINT", "SKIP_TASK", "UNSURE_OR_GIVE_UP",
    "SWITCH_MODE", "SHOW_PROGRESS", "GENERAL_CHAT",
]

_DETERMINISTIC_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "REQUEST_TASK": (
        "考考我", "检测", "出题", "出一道", "出几道", "几道题", "下一题",
        "给我一道", "来一道", "综合题", "再来一题", "再出", "测一下", "quiz",
    ),
    "SHOW_PROGRESS": ("学习状态", "进度", "我学到", "掌握"),
    "SWITCH_MODE": ("安静阅读", "深度学习", "复习模式", "评估模式", "切换模式"),
    "START_LEARNING": ("开始学习", "从第一章", "从头开始", "带我学", "开始吧"),
}

# P1-14: when a task is pending, a non-slash message is treated as an answer by
# default. But explicit help/explanation requests should NOT be judged as an
# answer — they are a request for an explanation. These cues override the
# pending-task default so "我不懂，先解释一下" is not misrouted to the judge.
_HELP_CUES = (
    "不懂", "解释", "讲一下", "讲解", "为什么", "什么是", "是什么", "什么意思",
    "提示", "再想想", "跳过", "换题", "太难", "不会", "不知道", "不清楚",
    "没学会", "忘了",
)

_QUESTION_CUES = ("?", "？", "怎么", "怎样", "如何", "能否", "可以告诉", "请解释")

_INTENT_VALUES = {
    "ASK_BOOK", "START_LEARNING", "REQUEST_EXPLANATION", "REQUEST_TASK",
    "SUBMIT_ANSWER", "REQUEST_HINT", "SKIP_TASK", "UNSURE_OR_GIVE_UP",
    "SWITCH_MODE", "SHOW_PROGRESS", "GENERAL_CHAT",
}


def _public_turn_error() -> str:
    return "这次没有处理成功。你的消息已经保留，可以稍后重试或换一种说法。"


def classify_intent(text: str, *, has_pending_task: bool = False) -> Intent:
    """Deterministic intent classification (PRODUCTIZATION §6.2 fallback).

    Priority: explicit command keywords win; a pending task + non-command input
    is normally SUBMIT_ANSWER, but an explicit help/explanation cue (P1-14)
    routes to REQUEST_EXPLANATION instead so the learner is not forced to guess.
    A real LLM classifier can override this when available; this rule is the
    guaranteed-safe floor that never fails.
    """
    t = text.strip()
    if not t:
        return "GENERAL_CHAT"
    if t.startswith("/"):
        return "GENERAL_CHAT"
    for intent, kws in _DETERMINISTIC_INTENT_KEYWORDS.items():
        if any(k in t for k in kws):
            return intent  # type: ignore[return-value]
    compact = "".join(t.split()).lower()
    if has_pending_task and compact in {"跳过", "跳过这题", "换题", "下一题"}:
        return "SKIP_TASK"
    if has_pending_task and compact in {"我不知道", "不知道", "不会", "不清楚", "没思路", "想不出来"}:
        return "UNSURE_OR_GIVE_UP"
    if has_pending_task and any(c in t for c in ("提示", "给点方向", "给个思路", "引导一下")):
        return "REQUEST_HINT"
    # P1-14: an explicit help/explanation cue takes priority over treating the
    # message as a task answer, even when a task is pending.
    if any(c in t for c in _HELP_CUES):
        return "REQUEST_EXPLANATION"
    if has_pending_task:
        # A learner may ask for clarification while a task is pending. Do not
        # silently grade a question as if it were an answer.
        if any(c in t for c in _QUESTION_CUES):
            return "REQUEST_EXPLANATION"
        return "SUBMIT_ANSWER"
    # Default: treat as a textbook question.
    return "ASK_BOOK"


def interpret_intent(
    router: ModelRouter, text: str, *, has_pending_task: bool, task_prompt: str = "",
) -> Intent:
    """Classify natural task actions with DeepSeek, with an offline-safe floor."""
    fallback = classify_intent(text, has_pending_task=has_pending_task)
    if not getattr(router.cfg, "live", False):
        return fallback
    prompt = task_prompt[:900] if task_prompt else "（当前没有待完成题目）"
    system = (
        "你是学习产品的意图识别器。只输出 JSON："
        '{"intent":"...","confidence":0-1}。'
        "intent 只能是 ASK_BOOK、START_LEARNING、REQUEST_EXPLANATION、REQUEST_TASK、"
        "SUBMIT_ANSWER、REQUEST_HINT、SKIP_TASK、UNSURE_OR_GIVE_UP、SWITCH_MODE、"
        "SHOW_PROGRESS、GENERAL_CHAT。不要回答教材内容。"
        "有待完成题时：明确要求提示→REQUEST_HINT；明确跳过/换题→SKIP_TASK；"
        "明确要讲解→REQUEST_EXPLANATION；仅表示不会、没思路、我不知道→UNSURE_OR_GIVE_UP；"
        "给出解答或推理→SUBMIT_ANSWER。"
    )
    res = router.complete(
        "action_interpretation",
        [{"role": "system", "content": system}, {
            "role": "user",
            "content": f"当前是否有待完成题：{has_pending_task}\n题目：{prompt}\n用户输入：{text[:600]}",
        }],
        output_schema={"type": "object", "required": ["intent", "confidence"]},
        temperature=0.0,
        max_tokens=240,
    )
    parsed = res.parsed_json if res.ok else None
    if not isinstance(parsed, dict):
        return fallback
    intent = str(parsed.get("intent") or "")
    try:
        confidence = float(parsed.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    # With no outstanding exercise, the deterministic floor deliberately
    # treats ordinary learner prose as a textbook question.  An intent-model
    # “GENERAL_CHAT” must not turn a concrete short query such as “列表是
    # 什么” into a generic chat acknowledgement; it loses both the answer and
    # the QUESTION evidence.  GENERAL_CHAT remains available for explicit
    # slash commands and empty input, which the deterministic floor already
    # handles above.
    if fallback == "ASK_BOOK" and intent == "GENERAL_CHAT":
        return "ASK_BOOK"
    if intent in _INTENT_VALUES and confidence >= 0.72:
        return intent  # type: ignore[return-value]
    return fallback


# --- run event helpers ----------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _evt(run_id: str, sequence: int, event_type: EventType, payload: dict | None = None) -> RunEvent:
    return RunEvent(
        run_id=run_id, sequence=sequence, event_type=event_type.value,
        payload=payload or {}, created_at=_now(),
    )


@dataclass
class TurnResult:
    """The outcome of processing one user message."""
    run: Run
    user_message: Message
    assistant_message: Message
    events: list[RunEvent] = field(default_factory=list)


class ConversationGraphState(TypedDict, total=False):
    """State carried through one LangGraph-supervised conversation turn.

    Domain objects remain in BookMind's repository; this state only coordinates
    the nodes in a single turn and therefore does not duplicate persistence.
    """

    conversation: Conversation
    user_text: str
    learner_id: str
    project_id: str
    run_id: str
    history: list[Message]
    source_context: dict
    intent: Intent
    seq: int
    events: list[RunEvent]
    blocks: list[ContentBlock]
    error: str
    event_sink: Callable[[RunEvent], None]


class ConversationOrchestrator:
    """Coordinates one conversation turn. Stateless across turns — all state
    lives in the repository."""

    def __init__(
        self,
        repo: Repository,
        router: ModelRouter,
        qa_service: BookQAService,
        task_service: "TaskService | None" = None,
    ) -> None:
        self.repo = repo
        self.router = router
        self.qa_service = qa_service
        if task_service is None:
            from ..agents.diagnostician import DiagnosticianAgent
            from ..services.task_service import TaskService
            task_service = TaskService(repo, router, DiagnosticianAgent(router))
        self.task_service = task_service
        self.workflow = self._build_workflow()

    # --- public API --------------------------------------------------------

    def process_message(
        self,
        *,
        conversation: Conversation,
        user_text: str,
        learner_id: str,
        project_id: str,
        idempotency_key: str = "",
        history: list[Message] | None = None,
        source_context: dict | None = None,
        run_id: str | None = None,
        user_message_id: str | None = None,
        assistant_message_id: str | None = None,
        event_sink: Callable[[RunEvent], None] | None = None,
    ) -> TurnResult:
        """Process one user message end-to-end and return the run + messages
        + events. Synchronous; the SSE endpoint runs this and streams events."""
        events: list[RunEvent] = []
        seq = 0

        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        user_msg_id = user_message_id or f"msg_{uuid.uuid4().hex[:12]}"
        asst_msg_id = assistant_message_id or f"msg_{uuid.uuid4().hex[:12]}"

        # 1. user message
        user_blocks = [ContentBlock(type="text", text=user_text)]
        selection_text = (source_context or {}).get("selection_text", "").strip()
        if selection_text:
            user_blocks.append(ContentBlock(type="context", data={
                "kind": "selection_context",
                "quote": selection_text,
                "source_id": (source_context or {}).get("source_id", ""),
                "page": (source_context or {}).get("page"),
            }))
        user_msg = Message(
            message_id=user_msg_id, conversation_id=conversation.conversation_id,
            role="user",
            content_blocks=user_blocks,
        )
        # 2. run
        run = Run(
            run_id=run_id, conversation_id=conversation.conversation_id,
            message_id=user_msg_id, status="RUNNING",
            idempotency_key=idempotency_key or run_id,
            started_at=_now(),
        )
        # 3. queued / started
        events.append(_evt(run_id, seq, EventType.RUN_STARTED, {"user_message_id": user_msg_id}))
        seq += 1

        # 4-5. LangGraph owns intent classification and conditional routing.
        # Existing services remain the business-logic nodes, so the migration
        # does not weaken BookMind's deterministic learning/evidence rules.
        blocks: list[ContentBlock] = []
        try:
            graph_result = self.workflow.invoke({
                "conversation": conversation,
                "user_text": user_text,
                "learner_id": learner_id,
                "project_id": project_id,
                "run_id": run_id,
                "history": history or [],
                "source_context": source_context or {},
                "seq": seq,
                "events": events,
                "blocks": [],
                "error": "",
                "event_sink": event_sink,
            })
            run.intent = graph_result["intent"]
            blocks = graph_result.get("blocks", [])
            events = graph_result.get("events", events)
            seq = graph_result.get("seq", seq)
            if graph_result.get("error"):
                run.status = "FAILED"
                run.error = graph_result["error"]
        except Exception as e:  # defensive: a turn failure is recoverable
            import logging
            logging.getLogger("bookmind").exception("Conversation turn failed")
            run.status = "FAILED"
            run.error = str(e)
            events.append(_evt(run_id, seq, EventType.RUN_FAILED, {"error": _public_turn_error()}))
            blocks = [ContentBlock(type="error", text=_public_turn_error())]

        # 6. assistant message
        assistant_msg = Message(
            message_id=asst_msg_id, conversation_id=conversation.conversation_id,
            role="assistant", content_blocks=blocks, run_id=run_id,
        )

        # 7. complete
        if run.status != "FAILED":
            run.status = "COMPLETED"
            run.completed_at = _now()
        events.append(_evt(run_id, seq, EventType.RUN_COMPLETED, {
            "message_id": asst_msg_id, "run_status": run.status,
        }))

        return TurnResult(run=run, user_message=user_msg, assistant_message=assistant_msg, events=events)

    # --- LangGraph workflow ------------------------------------------------

    def _build_workflow(self):
        """Compile the explicit, inspectable graph used for every user turn."""
        from langgraph.graph import END, START, StateGraph

        routes: dict[Intent, str] = {
            "ASK_BOOK": "book_qa",
            "START_LEARNING": "start_learning",
            "REQUEST_EXPLANATION": "explain",
            "REQUEST_TASK": "generate_task",
            "SUBMIT_ANSWER": "diagnose_answer",
            "REQUEST_HINT": "task_hint",
            "SKIP_TASK": "skip_task",
            "UNSURE_OR_GIVE_UP": "task_options",
            "SWITCH_MODE": "switch_mode",
            "SHOW_PROGRESS": "show_progress",
            "GENERAL_CHAT": "general_chat",
        }
        builder = StateGraph(ConversationGraphState)
        builder.add_node("classify_intent", self._graph_classify_intent)
        for intent, node_name in routes.items():
            builder.add_node(node_name, partial(self._graph_execute_intent, forced_intent=intent))
            builder.add_edge(node_name, END)
        builder.add_edge(START, "classify_intent")
        builder.add_conditional_edges(
            "classify_intent",
            lambda state: state["intent"],
            path_map=routes,
        )
        return builder.compile()

    def workflow_mermaid(self) -> str:
        """Expose the compiled topology for tests, documentation and demos."""
        return self.workflow.get_graph().draw_mermaid()

    def _graph_classify_intent(self, state: ConversationGraphState) -> dict:
        pending = self.repo.pending_task_for_conversation(
            state["conversation"].conversation_id,
        )
        intent = interpret_intent(
            self.router,
            state["user_text"],
            has_pending_task=pending is not None,
            task_prompt=str((pending or {}).get("prompt_text") or ""),
        )
        events = [*state["events"], _evt(
            state["run_id"], state["seq"], EventType.ACTION_SELECTED,
            {"intent": intent, "workflow": "langgraph"},
        )]
        return {"intent": intent, "events": events, "seq": state["seq"] + 1}

    def _graph_execute_intent(
        self,
        state: ConversationGraphState,
        *,
        forced_intent: Intent,
    ) -> dict:
        """Run one routed business node and convert failures into graph state."""
        events = list(state["events"])
        seq = state["seq"]
        blocks: list[ContentBlock]
        conversation = state["conversation"]
        try:
            if forced_intent == "ASK_BOOK":
                blocks, seq = self._ask_book(
                    project_id=state["project_id"], learner_id=state["learner_id"],
                    conversation_id=conversation.conversation_id,
                    question=_rewrite_followup(state["user_text"], state["history"]),
                    run_id=state["run_id"], seq=seq, events=events,
                    source_context=state.get("source_context", {}),
                    event_sink=state.get("event_sink"),
                )
            elif forced_intent == "START_LEARNING":
                blocks, seq = self._start_learning(
                    project_id=state["project_id"], learner_id=state["learner_id"],
                    conversation_id=conversation.conversation_id,
                    run_id=state["run_id"], seq=seq, events=events,
                )
            elif forced_intent == "SWITCH_MODE":
                mode = _mode_from_text(state["user_text"])
                if mode is None:
                    blocks = [ContentBlock(
                        type="text",
                        text="你想切换到哪一种？可以说：安静阅读、深度学习、复习模式或评估模式。",
                    )]
                else:
                    self.repo.update_project(state["project_id"], default_mode=mode)
                    events.append(_evt(
                        state["run_id"], seq, EventType.MODE_SELECTED, {"mode": mode.value},
                    ))
                    seq += 1
                    blocks = [ContentBlock(type="text", text=f"已切换到{_mode_label(mode)}。")]
            elif forced_intent == "SHOW_PROGRESS":
                blocks = self._show_progress(project_id=state["project_id"])
            elif forced_intent == "REQUEST_TASK":
                blocks, seq = self._request_task(
                    project_id=state["project_id"], learner_id=state["learner_id"],
                    conversation_id=conversation.conversation_id,
                    user_text=state["user_text"], run_id=state["run_id"],
                    seq=seq, events=events,
                )
            elif forced_intent == "SUBMIT_ANSWER":
                blocks, seq = self._submit_answer(
                    project_id=state["project_id"], learner_id=state["learner_id"],
                    conversation_id=conversation.conversation_id,
                    user_text=state["user_text"], run_id=state["run_id"],
                    seq=seq, events=events,
                )
            elif forced_intent == "REQUEST_HINT":
                pending = self.repo.pending_task_for_conversation(conversation.conversation_id)
                if pending is None:
                    blocks = [ContentBlock(type="text", text="当前没有待完成的题目。你可以先开始一道练习题。")]
                else:
                    hint = self.task_service.request_hint(pending["task_id"])
                    blocks = [
                        ContentBlock(type="status", text=hint["hint_notice"]),
                        ContentBlock(type="text", text=hint["hint_text"]),
                    ]
            elif forced_intent == "SKIP_TASK":
                pending = self.repo.pending_task_for_conversation(conversation.conversation_id)
                if pending is None:
                    blocks = [ContentBlock(type="text", text="当前没有待完成的题目。")]
                else:
                    task_id = pending["task_id"]
                    self.task_service.skip_task(task_id)
                    blocks = [
                        ContentBlock(type="text", text="已跳过这道题，不会把它记为错误答案。"),
                        ContentBlock(type="task", data={
                            "kind": "task_complete", "task_id": task_id,
                            "completion_status": "SKIPPED",
                            "source_scope": self.task_service.source_scope_for_task(task_id),
                        }),
                    ]
            elif forced_intent == "UNSURE_OR_GIVE_UP":
                pending = self.repo.pending_task_for_conversation(conversation.conversation_id)
                if pending is None:
                    blocks = [ContentBlock(type="text", text="当前没有待完成的题目。")]
                else:
                    blocks = [ContentBlock(type="task", data={
                        "kind": "task_options", "task_id": pending["task_id"],
                        "prompt_text": pending.get("prompt_text", ""),
                    })]
            elif forced_intent == "REQUEST_EXPLANATION":
                pending = self.repo.pending_task_for_conversation(conversation.conversation_id)
                if pending is not None:
                    explanation = self.task_service.explain_task(pending["task_id"])
                    source_scope = explanation.get("source_scope") or []
                    blocks = []
                    if source_scope:
                        blocks.append(ContentBlock(type="context", data={
                            "kind": "answer_context", "scope": "本题资料定位",
                            "reason": "讲解依据本题的题干、标准答案和判分要点生成；资料定位仅供回原文复习。",
                            "items": source_scope,
                        }))
                    blocks.extend([
                        ContentBlock(type="status", text="已展示讲解并结束本题；这次不会记为作答证据。"),
                        ContentBlock(type="text", text=explanation["text"]),
                        ContentBlock(type="task", data={
                            "kind": "task_complete", "task_id": pending["task_id"],
                            "completion_status": "EXPLAINED", "source_scope": source_scope,
                        }),
                    ])
                else:
                    blocks, seq = self._ask_book(
                        project_id=state["project_id"], learner_id=state["learner_id"],
                        conversation_id=conversation.conversation_id,
                        question=_rewrite_followup(state["user_text"], state["history"]),
                        run_id=state["run_id"], seq=seq, events=events,
                        source_context=state.get("source_context", {}),
                        event_sink=state.get("event_sink"),
                    )
                if not blocks:
                    blocks = [ContentBlock(
                        type="text",
                        text=("我先把这条记下来了。你可以直接询问资料里的内容，"
                              "比如“这部分的核心观点是什么”，或先添加一份学习资料。"),
                    )]
            else:
                blocks = [ContentBlock(
                    type="text",
                    text=("我先把这条记下来了。目前我擅长基于你的资料回答问题——"
                          "你可以直接问某个概念、段落或页面，也可以让我归纳和举例。"),
                )]
            return {"blocks": blocks, "events": events, "seq": seq}
        except Exception as exc:  # node failure is an explicit recoverable state
            import logging
            logging.getLogger("bookmind").exception("Conversation workflow node failed")
            events.append(_evt(
                state["run_id"], seq, EventType.RUN_FAILED, {"error": _public_turn_error()},
            ))
            return {
                "blocks": [ContentBlock(type="error", text=_public_turn_error())],
                "events": events,
                "seq": seq + 1,
                "error": str(exc),
            }

    # --- intent handlers ---------------------------------------------------

    def _ask_book(
        self, *, project_id: str, learner_id: str, conversation_id: str = "", question: str,
        run_id: str, seq: int, events: list[RunEvent],
        source_context: dict | None = None,
        event_sink: Callable[[RunEvent], None] | None = None,
    ) -> tuple[list[ContentBlock], int]:
        """ASK_BOOK path: retrieve → context → Tutor → citation validation.
        Reuses BookQAService.ask(). Emits tool_started/tool_completed/agent_*."""
        started_event = _evt(run_id, seq, EventType.TOOL_STARTED, {"tool": "retrieval"})
        events.append(started_event)
        if event_sink:
            event_sink(started_event)
        seq += 1

        source_context = source_context or {}
        scope = source_context.get("scope", "ALL_SOURCES")
        selected_source = source_context.get("source_id") or ""
        selected_page = source_context.get("page")
        selection_text = (source_context.get("selection_text") or "").strip()
        source_ids = [selected_source] if selected_source and scope in ("CURRENT_SOURCE", "CURRENT_PAGE") else None
        physical_page = selected_page if scope == "CURRENT_PAGE" else None
        retrieval_question = (
            f"{question}\n\n用户选中的原文：\n{selection_text}"
            if selection_text else question
        )
        def locations_ready(locations: list[dict], confidence: float, grounded: bool) -> None:
            nonlocal seq
            event = _evt(run_id, seq, EventType.SOURCE_LOCATIONS_READY, {
                "locations": locations, "confidence": confidence, "grounded": grounded,
            })
            events.append(event)
            if event_sink:
                event_sink(event)
            seq += 1

        ans = self.qa_service.ask(
            project_id=project_id, learner_id=learner_id, question=retrieval_question,
            source_ids=source_ids, physical_page=physical_page,
            on_retrieval=locations_ready,
        )

        # A grounded textbook question is an exposure/doubt signal, never a
        # mastery verdict. Record only concepts that can be mapped back to the
        # retrieved chunks (with a conservative name fallback for the bundled
        # demo graph, whose legacy gold nodes predate SourceRef anchoring).
        questioned_concepts = self._record_question_signals(
            project_id=project_id,
            learner_id=learner_id,
            conversation_id=conversation_id,
            question=question,
            run_id=run_id,
            chunk_ids=ans.chunk_ids if ans.grounded else [],
            retrieval_confidence=ans.retrieval_confidence,
            query_scope=scope,
            selected_page=selected_page,
        ) if source_context.get("record_question_signal", True) else []

        # When a graph node is directly identified, use its trusted source
        # anchors to correct a broad retrieval before rendering the answer.
        # Example: after a KMP question, “列表是什么” must not be answered
        # from KMP chunks merely because they scored highly in BM25.  Current
        # page remains a hard boundary: we report the location mismatch there
        # instead of silently reading material from another page.
        anchor_ids = [
            chunk_id
            for item in questioned_concepts[:1]
            for chunk_id in item.get("chunk_ids", [])
        ]
        answer_ids = set(ans.chunk_ids)
        if (
            anchor_ids
            and scope != "CURRENT_PAGE"
            and not answer_ids.intersection(anchor_ids)
        ):
            canonical_name = str(questioned_concepts[0].get("name") or "")
            ans = self.qa_service.ask(
                project_id=project_id,
                learner_id=learner_id,
                question=f"{canonical_name}\n{retrieval_question}",
                source_ids=source_ids,
                preferred_chunk_ids=anchor_ids,
                on_retrieval=locations_ready,
            )

        events.append(_evt(run_id, seq, EventType.TOOL_COMPLETED, {
            "tool": "retrieval", "grounded": ans.grounded, "chunk_ids": ans.chunk_ids,
        }))
        seq += 1
        events.append(_evt(run_id, seq, EventType.RETRIEVAL_COMPLETED, {
            "grounded": ans.grounded, "confidence": ans.retrieval_confidence,
            "chunk_ids": ans.chunk_ids,
        }))
        seq += 1

        # Streaming-ish: emit the answer text as one agent_delta (the offline
        # path returns the full text at once; a live model could chunk it).
        blocks: list[ContentBlock] = []
        if ans.chunk_ids:
            context_items = []
            seen = set()
            for chunk_id in ans.chunk_ids:
                chunk = self.repo.chunk_by_id(chunk_id)
                if chunk is None:
                    continue
                source = self.repo.get_source(chunk.book_id)
                title = source.title if source else "学习资料"
                key = (chunk.book_id, chunk.short_label())
                if key in seen:
                    continue
                seen.add(key)
                context_items.append({
                    "source_id": chunk.book_id,
                    "title": title,
                    "locator": chunk.short_label(),
                })
            scope_labels = {
                "CURRENT_PAGE": "当前页面",
                "CURRENT_SOURCE": "当前资料",
                "ALL_SOURCES": "全部资料",
            }
            blocks.append(ContentBlock(type="context", data={
                "kind": "answer_context",
                "scope": scope_labels.get(scope, "全部资料"),
                "reason": (
                    "结合你选中的原文，并从当前页面检索相关片段后作答。"
                    if selection_text else "从所选范围中检索与本次问题最相关的原文片段后作答。"
                ),
                "items": context_items[:4],
            }))
        if ans.answer_text:
            events.append(_evt(run_id, seq, EventType.AGENT_DELTA, {"text": ans.answer_text}))
            seq += 1
            events.append(_evt(run_id, seq, EventType.ANSWER_DELTA, {"text": ans.answer_text}))
            seq += 1
            blocks.append(ContentBlock(type="text", text=ans.answer_text))
        terminal_answer_event = (
            EventType.ANSWER_UNAVAILABLE if ans.fallback else EventType.ANSWER_COMPLETED
        )
        events.append(_evt(run_id, seq, terminal_answer_event, {
            "fallback": ans.fallback, "grounded": ans.grounded,
        }))
        seq += 1

        if questioned_concepts:
            if scope == "CURRENT_PAGE" and selected_page:
                mapped = questioned_concepts[0]
                target_page = int(mapped.get("source_page") or 0)
                if target_page and target_page != selected_page:
                    blocks.append(ContentBlock(type="status", text=(
                        f"当前第 {selected_page} 页不包含“{mapped['name']}”的正文；"
                        f"它位于第 {target_page} 页附近。已记录为待验证知识点，"
                        "可切换到对应章节继续查看。"
                    )))
            preview = "、".join(item["name"] for item in questioned_concepts[:3])
            suffix = "等" if len(questioned_concepts) > 3 else ""
            blocks.append(ContentBlock(
                type="question_signal",
                data={
                    "signal_id": f"question_{run_id}",
                    "concepts": questioned_concepts,
                    "message": (
                        f"已记录你对“{preview}{suffix}”有过疑问。"
                        "这只会进入待验证队列，不代表你不会，也不会改变掌握状态。"
                    ),
                },
            ))
        elif ans.grounded:
            blocks.append(ContentBlock(
                type="question_signal",
                data={
                    "signal_id": f"question_unclassified_{run_id}",
                    "concepts": [],
                    "unclassified": True,
                    "message": (
                        "已保留这次提问，但暂时无法可靠对应到具体知识点，"
                        "因此不会写入学习状态。"
                    ),
                },
            ))

        # Citations as structured blocks + citation_attached events.
        for i, c in enumerate(ans.citations):
            chunk_id = c.get("chunk_id", "")
            # Resolve the book_id + physical page from the chunk so the Reader
            # can open the right PDF at the right page (M3).
            chunk = self.repo.chunk_by_id(chunk_id) if chunk_id else None
            book_id = c.get("book_id") or (chunk.book_id if chunk else "")
            page = c.get("page") or (str(chunk.source_ref.physical_page) if chunk else "")
            source = self.repo.get_source(book_id) if book_id else None
            title = source.title if source else "学习资料"
            locator = chunk.short_label() if chunk else f"p.{page}"
            events.append(_evt(run_id, seq, EventType.CITATION_ATTACHED, {
                "index": i + 1, "chunk_id": chunk_id, "page": page, "book_id": book_id,
            }))
            seq += 1
            blocks.append(ContentBlock(
                type="citation", chunk_id=chunk_id,
                quote=c.get("quote", ""), page=str(page),
                book_id=book_id, label=f"[{i+1}] {title} · {locator}",
            ))

        if ans.fallback or not ans.grounded:
            events.append(_evt(run_id, seq, EventType.FALLBACK_USED, {"reason": ans.reason}))
            seq += 1
            if not ans.answer_text:
                blocks.append(ContentBlock(
                    type="text",
                    text="在当前资料范围中没有找到足够依据。你可以扩大提问范围、换一种问法，或先添加学习资料。",
                ))

        return blocks, seq

    def _record_question_signals(
        self,
        *,
        project_id: str,
        learner_id: str,
        conversation_id: str,
        question: str,
        run_id: str,
        chunk_ids: list[str],
        retrieval_confidence: float = 0.0,
        query_scope: str = "ALL_SOURCES",
        selected_page: int | None = None,
    ) -> list[dict]:
        """Persist QUESTION evidence for conservatively matched concepts.

        QUESTION is exposure-only: Evidence Gate rejects it for mastery and
        the learner-state grouping never treats it as a failed assessment.
        Failure to write this auxiliary signal must not fail the user's answer.
        """
        import hashlib
        import logging
        import re
        from ..services.learning_memory import remember_question_context

        retrieved = [self.repo.chunk_by_id(chunk_id) for chunk_id in chunk_ids[:4]]
        retrieved = [chunk for chunk in retrieved if chunk is not None]
        chunk_order = {chunk.chunk_id: index for index, chunk in enumerate(retrieved)}
        question_folded = question.casefold()

        def question_match_score(name: str, *, permit_short_exact: bool = False) -> int:
            """Return a strict lexical score; nearby retrieval alone is insufficient."""
            folded = name.casefold().strip()
            if not folded:
                return 0
            terms: set[str] = set()
            # Two-character Chinese terms are common, valid textbook concepts
            # (for example “列表”).  They are too broad for generic substring
            # matching in descriptions, but are safe when they are the exact
            # canonical name or section label of a graph node.
            if permit_short_exact:
                terms.update(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z][a-z0-9_+#.-]{1,}", folded))
            if len(folded) >= 3:
                terms.add(folded)
            for part in re.findall(r"[\u4e00-\u9fff]{3,}|[a-z][a-z0-9_+#.-]{2,}", folded):
                terms.add(part)
                if re.fullmatch(r"[\u4e00-\u9fff]+", part):
                    terms.update(
                        part[i:i + width]
                        for width in range(3, min(6, len(part)) + 1)
                        for i in range(len(part) - width + 1)
                    )
            return max((len(term) for term in terms if term in question_folded), default=0)

        matches: list[tuple[int, int, int, object, list[str], str]] = []
        # Mapping a learner's topic to the knowledge graph must not depend on
        # the current-page retrieval succeeding.  “KMP 是什么” while reading
        # vectors should still record KMP as a QUESTION signal, while the
        # answer itself remains correctly restricted to the selected page.
        from .concept_scope import is_learning_concept

        for book_id in sorted(self.repo.allowed_book_ids(project_id)):
            book_chunks = [chunk for chunk in retrieved if chunk.book_id == book_id]
            retrieved_ids = {chunk.chunk_id for chunk in book_chunks}
            for concept in self.repo.concepts_for_book(book_id):
                if not is_learning_concept(concept):
                    continue
                anchored = [
                    ref.chunk_id for ref in concept.source_refs
                    if ref.chunk_id and ref.chunk_id in retrieved_ids
                ]
                name = concept.name.strip()
                direct_score = max(
                    question_match_score(name, permit_short_exact=True),
                    question_match_score(concept.section or "", permit_short_exact=True),
                    question_match_score(concept.description or ""),
                )
                if not anchored and not direct_score:
                    continue
                supporting = anchored or [
                    ref.chunk_id for ref in concept.source_refs if ref.chunk_id
                ][:3]
                # Older/seeded graph nodes may lack SourceRef.chunk_id.  A
                # direct name match should still leave an auditable route back
                # to the source instead of creating a QUESTION record with no
                # usable reference.  This is a conservative lexical fallback
                # within the same book only; it never contributes mastery.
                if not supporting and direct_score:
                    concept_terms = {
                        term for term in re.findall(
                            r"[\u4e00-\u9fff]{2,}|[a-z][a-z0-9_+#.-]{1,}",
                            f"{concept.name} {concept.section}".casefold(),
                        ) if len(term) >= 2
                    }
                    supporting = [
                        chunk.chunk_id
                        for chunk in self.repo.chunks_for_project(project_id)
                        if chunk.book_id == book_id
                        and any(term in chunk.content.casefold() for term in concept_terms)
                    ][:3]
                rank = 0 if direct_score else 1
                # A graph concept can legitimately have a page anchor but no
                # extracted chunk id yet.  A direct lexical match remains a
                # valid QUESTION signal in that case; it simply has no
                # retrieval-rank tiebreaker.
                first_chunk = min(
                    (chunk_order.get(chunk_id, 99) for chunk_id in supporting),
                    default=99,
                )
                matches.append((rank, first_chunk, direct_score, concept, supporting, book_id))

        # Short exact canonical terms receive a deliberate high score so they
        # are not confused with incidental two-character prose fragments.
        direct = [item for item in matches if item[2] >= 2]
        chosen: list[tuple[int, int, int, object, list[str], str]] = []
        if direct:
            chosen = [sorted(direct, key=lambda item: (
                -item[2], item[1], -item[3].importance, item[3].name,
            ))[0]]
        elif matches and getattr(self.router.cfg, "live", False):
            candidates = sorted(matches, key=lambda item: (
                item[1], -item[3].importance, item[3].name,
            ))[:10]
            candidate_text = "\n".join(
                f"- id={item[3].concept_id}; 名称={item[3].name}; 说明={item[3].description[:180]}"
                for item in candidates
            )
            try:
                result = self.router.complete(
                    "question_concept_classification",
                    [{"role": "system", "content": (
                        "你负责把学习者的问题保守地归到一个知识点。只能从候选中选择一个；"
                        "若问题只是该章节附近但没有明确语义关联，必须返回 null。"
                        "输出严格 JSON：{\"concept_id\":\"候选 id 或 null\",\"confidence\":0到1}。"
                    )}, {"role": "user", "content": (
                        f"学习者问题：{question[:700]}\n候选知识点：\n{candidate_text}"
                    )}],
                    output_schema={"type": "object"},
                    temperature=0.0,
                    max_tokens=180,
                )
                parsed = result.parsed_json if result.ok else None
                concept_id = str((parsed or {}).get("concept_id") or "")
                confidence = float((parsed or {}).get("confidence") or 0)
                if confidence >= 0.78:
                    chosen = [item for item in candidates if item[3].concept_id == concept_id][:1]
            except (TypeError, ValueError, AttributeError):
                chosen = []

        recorded: list[dict] = []
        seen: set[str] = set()
        for _, _, match_score, concept, supporting, book_id in chosen:
            if concept.concept_id in seen:
                continue
            seen.add(concept.concept_id)
            digest = hashlib.sha256(
                f"{run_id}|{concept.concept_id}|QUESTION".encode("utf-8"),
            ).hexdigest()[:24]
            try:
                record_exposure(
                    self.repo,
                    learner_id=learner_id,
                    project_id=project_id,
                    concept_id=concept.concept_id,
                    source_book_id=book_id,
                    evidence_id=f"q_{digest}",
                    evidence_type=EvidenceType.QUESTION,
                    occurred_at=_now(),
                    source_chunk_ids=supporting,
                    source_session=run_id,
                    content_summary=(
                        f"question:{question[:500]}|confidence:{retrieval_confidence:.4f}"
                        f"|scope:{query_scope}|chunks:{','.join(supporting)}"
                    ),
                )
                remember_question_context(
                    self.repo, project_id=project_id, concept_id=concept.concept_id,
                    conversation_id=conversation_id, question=question, run_id=run_id,
                    confidence=max(retrieval_confidence, 1.0 if match_score else 0.78),
                )
                question_count = sum(
                    1 for evidence in self.repo.evidence_for(project_id, concept.concept_id)
                    if evidence.evidence_type == EvidenceType.QUESTION
                )
                recorded.append({
                    "concept_id": concept.concept_id,
                    "name": concept.name,
                    "question_count": question_count,
                    "chunk_ids": supporting,
                    "retrieval_confidence": retrieval_confidence,
                    "query_scope": query_scope,
                    "source_id": book_id,
                    "source_page": next((ref.physical_page for ref in concept.source_refs if ref.physical_page), 0),
                })
            except Exception:
                logging.getLogger("bookmind").exception(
                    "Failed to record QUESTION signal for %s", concept.concept_id,
                )
        return recorded

    def _show_progress(self, *, project_id: str) -> list[ContentBlock]:
        """SHOW_PROGRESS: a compact text summary of the learning state."""
        from ..services.learner_state_view import project_state_views
        from ..domain.models import ReviewPolicy
        views = project_state_views(self.repo, project_id, policy=ReviewPolicy())
        if not views:
            return [ContentBlock(type="text", text="还没有学习记录。先添加一份资料开始学习吧。")]
        groups: dict[str, int] = {}
        for v in views:
            groups[v.group] = groups.get(v.group, 0) + 1
        lines = [f"共 {len(views)} 个知识点："]
        label_map = {"verified": "已验证", "pending": "待验证", "weak": "薄弱", "due": "待复习"}
        for g, n in sorted(groups.items()):
            lines.append(f"  · {label_map.get(g, g)}：{n}")
        return [ContentBlock(type="text", text="\n".join(lines))]

    def _start_learning(
        self, *, project_id: str, learner_id: str, conversation_id: str,
        run_id: str, seq: int, events: list[RunEvent],
    ) -> tuple[list[ContentBlock], int]:
        """Start from the earliest mapped concept in the uploaded sources."""
        concepts = []
        for book_id in sorted(self.repo.allowed_book_ids(project_id)):
            concepts.extend(self.repo.concepts_for_book(book_id))
        if not concepts:
            return [ContentBlock(
                type="text", text="这个空间还没有可学习的知识点。请先添加资料，并等待知识范围整理完成。",
            )], seq
        first = min(
            concepts,
            key=lambda c: (c.chapter or "~", c.section or "~", -c.importance, c.name),
        )
        question = (
            f"请从资料对应位置讲解知识点“{first.name}”，说明它是什么、为什么重要，"
            "并给出一个便于初学者理解的例子。"
        )
        answer, seq = self._ask_book(
            project_id=project_id, learner_id=learner_id, conversation_id=conversation_id, question=question,
            run_id=run_id, seq=seq, events=events,
        )
        intro = ContentBlock(
            type="text",
            text=f"我们从“{first.name}”开始。它位于 {first.section or first.chapter or '资料开头'}。",
        )
        return [intro, *answer], seq

    # --- task / answer handlers (M4) --------------------------------------

    def _request_task(
        self, *, project_id: str, learner_id: str, conversation_id: str,
        user_text: str, run_id: str, seq: int, events: list[RunEvent],
    ) -> tuple[list[ContentBlock], int]:
        """REQUEST_TASK: ask the Engine what to do, generate+validate+persist a
        task, and emit a task card the browser renders."""
        events.append(_evt(run_id, seq, EventType.TOOL_STARTED, {"tool": "diagnosis"}))
        seq += 1

        payload = self.task_service.request_task(
            project_id=project_id, learner_id=learner_id,
            conversation_id=conversation_id, user_text=user_text,
        )

        events.append(_evt(run_id, seq, EventType.TOOL_COMPLETED, {
            "tool": "diagnosis", "kind": payload.get("kind"),
        }))
        seq += 1

        blocks: list[ContentBlock] = []
        if payload.get("kind") in ("fallback", "existing") or "task_id" not in payload:
            blocks.append(ContentBlock(type="text", text=payload.get("text", "暂时无法生成任务。")))
            return blocks, seq

        events.append(_evt(run_id, seq, EventType.AGENT_STARTED, {"agent": "diagnostician"}))
        seq += 1
        events.append(_evt(run_id, seq, EventType.AGENT_COMPLETED, {
            "agent": "diagnostician", "task_id": payload["task_id"],
        }))
        seq += 1

        blocks.append(ContentBlock(
            type="task",
            data={
                "kind": payload["kind"],
                "task_id": payload["task_id"],
                "prompt_text": payload["prompt_text"],
                "is_probe": payload.get("is_probe", False),
                "is_changed_task": payload.get("is_changed_task", False),
                "remediation_stage": payload.get("remediation_stage", 0),
                "focus": payload.get("focus", "当前知识点"),
                "source_scope": payload.get("source_scope", []),
                "generation_reason": payload.get("generation_reason", ""),
            },
        ))
        return blocks, seq

    def _submit_answer(
        self, *, project_id: str, learner_id: str, conversation_id: str,
        user_text: str, run_id: str, seq: int, events: list[RunEvent],
    ) -> tuple[list[ContentBlock], int]:
        """SUBMIT_ANSWER: the browser typed an answer while a task is pending.
        Delegate to TaskService (the same path POST /api/tasks/{id}/answer
        uses). If no pending task somehow exists, fall back to book Q&A."""
        pending = self.repo.pending_task_for_conversation(conversation_id)
        if pending is None:
            # Race: the task was answered/skipped in another tab. Treat the
            # input as an ordinary question.
            return self._ask_book(
                project_id=project_id, learner_id=learner_id, conversation_id=conversation_id, question=user_text,
                run_id=run_id, seq=seq, events=events, source_context={},
            )

        events.append(_evt(run_id, seq, EventType.TOOL_STARTED, {"tool": "diagnosis"}))
        seq += 1

        idem = f"sub_{uuid.uuid4().hex[:12]}"
        result = self.task_service.submit_answer(
            task_id=pending["task_id"], answer_text=user_text,
            idempotency_key=idem, learner_id=learner_id, run_id=run_id,
        )

        events.append(_evt(run_id, seq, EventType.TOOL_COMPLETED, {
            "tool": "diagnosis",
            "result": result.get("judgment", {}).get("result"),
        }))
        seq += 1

        blocks: list[ContentBlock] = [ContentBlock(
            type="task",
            data={
                "kind": "judgment",
                "task_id": pending["task_id"],
                "judgment": result.get("judgment", {}),
                "needs_review": result.get("needs_review", False),
                "written": result.get("written", False),
                "next_action_code": (result.get("next_action") or {}).get("selected_action"),
                "source_scope": self.task_service.source_scope_for_task(pending["task_id"]),
            },
        )]

        if result.get("evidence_id") and result.get("written"):
            events.append(_evt(run_id, seq, EventType.EVIDENCE_CREATED, {
                "evidence_id": result["evidence_id"],
                "task_id": pending["task_id"],
            }))
            seq += 1

        delta = result.get("state_delta") or {}
        transitions = (delta.get("mastery_transitions") or []) + (delta.get("misconception_transitions") or [])
        if transitions:
            events.append(_evt(run_id, seq, EventType.STATE_UPDATED, {
                "mastery_transitions": delta.get("mastery_transitions", []),
                "misconception_transitions": delta.get("misconception_transitions", []),
            }))
            seq += 1
            blocks.append(ContentBlock(
                type="state_change",
                data={
                    "mastery_transitions": delta.get("mastery_transitions", []),
                    "misconception_transitions": delta.get("misconception_transitions", []),
                },
            ))

        next_action = result.get("next_action")
        if result.get("needs_review"):
            blocks.append(ContentBlock(
                type="text",
                text=result.get("clarification") or "我还不能根据这句话判断你的理解。请补充你的思考过程，原题仍然保留。",
            ))
        elif next_action and next_action.get("selected_action"):
            blocks.append(ContentBlock(
                type="text", text=_next_action_copy(next_action.get("selected_action")),
            ))
        if not result.get("needs_review"):
            blocks.append(ContentBlock(type="task", data={
                "kind": "task_complete",
                "task_id": pending["task_id"],
                "completion_status": "ANSWERED",
                "source_scope": self.task_service.source_scope_for_task(pending["task_id"]),
            }))
        return blocks, seq

    # --- Next Best Action (read-only, for the sidebar) ---------------------

    def next_action_for_project(self, project_id: str) -> dict:
        """Return the Engine's Next Best Action for the sidebar (read-only;
        the Engine decides, the Orchestrator only surfaces it). The activity
        mode / intervention policy come from the project's ``default_mode``
        (PRODUCTIZATION M6) instead of a hardcoded READING/PROACTIVE."""
        views: list[ConceptView] = []
        for bid in self.repo.allowed_book_ids(project_id):
            for c in self.repo.concepts_for_book(bid):
                s = self.repo.get_state(project_id, c.concept_id)
                views.append(ConceptView(concept=c, state=s))
        mis = self.repo.all_misconceptions(project_id)
        preset = self.repo.get_project_mode(project_id) or UIPreset.QUIET_READING
        activity_mode, intervention_policy = dimensions_for(preset)
        inp = DecisionInput(
            activity_mode=activity_mode,
            intervention_policy=intervention_policy,
            ui_preset=preset.value,
            concepts=views, misconceptions=mis,
        )
        trace = decide(inp)
        return {
            "selected_action": trace.selected_action,
            "selected_concept_id": trace.selected_concept_id,
            "reason": trace.reason,
        }


def _rewrite_followup(text: str, history: list[Message]) -> str:
    """Resolve only genuinely deictic follow-ups against the prior question.

    A short question is not automatically a follow-up: ``列表是什么？`` after
    ``KMP 算法是什么？`` must retrieve lists, not KMP.  The old length-only
    rule made this very common interaction fail.  We preserve context only for
    explicit references such as “那它为什么…”, or for fragmentary questions
    without their own topic.
    """
    import re

    cleaned = text.strip()
    if not cleaned:
        return cleaned
    deictic_prefixes = ("那", "它", "这个", "上述", "前面", "刚才", "上面", "然后")
    if cleaned.startswith(deictic_prefixes):
        should_rewrite = True
    elif len(cleaned) > 24:
        should_rewrite = False
    else:
        # A self-contained question contains a concrete subject before an
        # interrogative form, e.g. “列表是什么”“链表和数组有什么区别”.
        direct_forms = ("是什么", "的定义", "有什么区别", "怎么实现", "如何实现", "时间复杂度", "适用", "作用", "特点")
        has_direct_form = any(form in cleaned for form in direct_forms)
        subject = re.split(r"是什么|的定义|有什么区别|怎么实现|如何实现|时间复杂度|适用|作用|特点|[？?]", cleaned, maxsplit=1)[0].strip()
        should_rewrite = not (has_direct_form and len(subject) >= 2)
    if not should_rewrite:
        return cleaned
    previous = ""
    for message in reversed(history):
        if message.role != "user":
            continue
        previous = " ".join(
            block.text for block in message.content_blocks
            if block.type == "text" and block.text
        ).strip()
        if previous:
            break
    if not previous:
        return cleaned
    return f"上一轮问题：{previous}\n本轮追问：{cleaned}"


def _mode_from_text(text: str) -> UIPreset | None:
    aliases = (
        (("安静", "阅读模式"), UIPreset.QUIET_READING),
        (("深度",), UIPreset.DEEP_LEARNING),
        (("复习",), UIPreset.REVIEW),
        (("评估", "考试"), UIPreset.ASSESSMENT),
    )
    for words, mode in aliases:
        if any(word in text for word in words):
            return mode
    return None


def _mode_label(mode: UIPreset) -> str:
    return {
        UIPreset.QUIET_READING: "安静阅读模式",
        UIPreset.DEEP_LEARNING: "深度学习模式",
        UIPreset.REVIEW: "复习模式",
        UIPreset.ASSESSMENT: "评估模式",
    }[mode]


def _next_action_copy(action: str | None) -> str:
    """Translate internal decision codes into learner-facing Chinese copy."""
    return {
        "VERIFY": "这道题已经完成。接下来可以继续检测相近的知识点。",
        "REVIEW": "这道题已经完成。接下来更适合复习一个还不够稳的知识点。",
        "DIAGNOSE": "我发现有个地方值得再确认，下一题会换个角度帮你辨清。",
        "REMEDIATE": "接下来会针对刚才容易混淆的地方练习一次。",
        "LEARN_PREREQUISITE": "继续前，建议先补一下相关的基础概念。",
        "WAIT": "这道题已经完成，你可以继续学习，或者主动开始下一次检测。",
    }.get(action or "", "这道题已经完成，可以继续学习或开始下一题。")
