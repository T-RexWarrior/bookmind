"""Task Service — the M4 bridge from the conversation orchestrator to the
Phase-5 task/diagnosis/misconception engine (PRODUCTIZATION §M4).

This service is the single place that:

  * asks the Learning Engine's Next Best Action for what to do next,
  * generates a task draft (probe / changed-task / quiz) accordingly,
  * runs it through the shared Task Validator,
  * persists the immutable TrustedTask (+ prompt/expected_answer/distractors),
  * counts hints server-side,
  * on answer: reconstructs the trusted task from storage (never the request
    body), asks the Diagnostician to judge, validates misconception signals,
    and commits the result through ``engine.submit_answer`` — the only state
    writer.

Hard contract (PRODUCTIZATION §5.7, §10.3): the browser only ever sends
``task_id`` + ``answer_text`` (+ idempotency_key). It never sends PASS/FAIL,
Concept IDs, rubric, hint counts, or misconception scores. All of those come
from the server-side trusted context. The Diagnostician never sets
required_level / hint_level / independent; the Engine derives them.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ..agents.bug_library import BUG_LIBRARY, BugEntry
from ..agents.diagnostician import DiagnosticianAgent
from ..errors import AppError
from ..domain.enums import (
    Action,
    EvidenceResult,
    JudgmentStatus,
    Level,
    UIPreset,
)
from ..domain.models import (
    AnswerJudgment,
    InteractionContext,
    RemediationPlan,
    ReviewPolicy,
    TaskDraft,
    TrustedTaskContext,
)
from ..engine.action_matrix import dimensions_for
from ..engine.decision.next_action import ConceptView, DecisionInput, decide
from ..engine.learning_engine import submit_answer as engine_submit_answer
from ..engine.misconception.probe_classifier import signals_for_probe
from ..engine.task.generator import (
    generate_changed_task,
    generate_probe,
    generate_quiz,
)
from ..engine.task.validator import validate
from ..llm.router import ModelRouter
from ..services.remediation import RemediationService
from ..storage.protocols import Repository

HINT_NOTICE = "本次可以继续练习，但不会作为独立掌握证据"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _level_from_str(s: str) -> Level:
    return Level(s)


class TaskService:
    """Application service bridging the orchestrator to the task engine."""

    def __init__(
        self,
        repo: Repository,
        router: ModelRouter,
        diagnostician: DiagnosticianAgent,
    ) -> None:
        self.repo = repo
        self.router = router
        self.diagnostician = diagnostician

    # --- task generation --------------------------------------------------

    def request_task(
        self,
        *,
        project_id: str,
        learner_id: str,
        conversation_id: str,
        user_text: str = "",
        concept_id: str = "",
        selection: str = "RECOMMENDED",
    ) -> dict:
        """Generate, validate, and persist one task for the project.

        Returns a TaskCardPayload safe to send to the browser (no rubric /
        expected_answer / concept_id / discriminated_bug_ids). On any failure
        returns ``{"kind": "fallback", "text": ...}`` rather than raising.
        """
        try:
            active_followup = self.repo.active_followup_task_for_conversation(conversation_id)
            if active_followup is not None:
                raise AppError(
                    "FOLLOWUP_ACTIVE",
                    "请先结束上一题的追问，再开始下一题。",
                    status_code=409,
                    action="END_FOLLOWUP",
                )
            pending = self.repo.pending_task_for_conversation(conversation_id)
            if pending is not None:
                payload = self.get_task(pending["task_id"]) or {}
                return {**payload, "kind": "existing", "existing": True,
                        "text": "上一道检测题还在等你的回答。你可以继续作答，或者跳过后再开始新题。"}
            # The user explicitly asked for a check ("考考我"): treat the decision
            # as user-requested so QUIET mode does not suppress it (§10).
            decision = self._next_action(project_id, user_requested=True)
            preferred = self._exact_concept(project_id, concept_id) if concept_id else None
            if concept_id and preferred is None:
                raise AppError("CONCEPT_NOT_IN_SCOPE", "这个知识点不在当前学习空间中", status_code=404)
            # The primary “开始推荐练习” button must use the same ordered queue
            # rendered in the left panel.  Previously RECOMMENDED skipped this
            # branch and fell back to the decision engine's first graph node.
            if preferred is None:
                if selection.upper() == "RANDOM":
                    preferred = self.random_unverified_concept(project_id)
                else:
                    candidate = next(iter(self.consolidation_candidates(
                        project_id, mode="PRACTICE", filter=selection,
                    )["candidates"]), None)
                    preferred = self._exact_concept(project_id, candidate["concept_id"]) if candidate else None
            if preferred is None:
                preferred = self._preferred_practice_concept(project_id, user_text)
            if preferred is not None:
                # An explicitly named concept, or a request to practise
                # something previously questioned, outranks the generic queue.
                # This only chooses the task target; mastery still changes only
                # after the learner submits an answer through Evidence Gate.
                decision = {
                    "selected_action": Action.VERIFY.value,
                    "selected_concept_id": preferred.concept_id,
                    "reason": "user-selected or previously-questioned concept → VERIFY",
                }
            action = decision.get("selected_action")
            concept_id = decision.get("selected_concept_id") or ""
            draft = self._draft_for_action(
                project_id, action, concept_id, strict_concept=preferred is not None,
            )
            if draft is None:
                return {"kind": "fallback",
                        "text": "现在没有合适的检测点。你可以先继续阅读或提问，我会在合适的时候请你检测。"}
            report = validate(draft, self.repo, project_id)
            if not report.passed or report.trusted is None:
                return {"kind": "fallback",
                        "text": "想出题但没能生成一道合规的题目，请稍后再试。"}
            self._persist_task(
                report.trusted, draft,
                project_id=project_id, learner_id=learner_id,
                conversation_id=conversation_id,
            )
            return self._card_payload(report.trusted, draft, decision, project_id)
        except AppError:
            raise
        except Exception:  # defensive: a turn failure is recoverable
            import logging
            logging.getLogger("bookmind").exception("Task generation failed")
            return {"kind": "fallback", "text": "这次没能准备好检测题，请稍后重试。"}

    def consolidation_candidates(
        self,
        project_id: str,
        *,
        mode: str = "PRACTICE",
        filter: str = "RECOMMENDED",
        source_id: str = "",
    ) -> dict:
        """Return the server-owned practice/assessment queue with reasons.

        QUESTION evidence raises a concept's priority but is never labelled as
        weakness.  The same queue powers both consolidation sub-modes; only the
        interaction policy changes when the task is created.
        """
        from ..services.learner_state_view import project_state_views

        mode = mode.upper()
        selected_filter = filter.upper()
        if mode not in {"PRACTICE", "ASSESSMENT"}:
            raise AppError("INVALID_MODE", "不支持的巩固模式", status_code=422)
        if selected_filter not in {"RECOMMENDED", "QUESTIONED", "WEAK", "DUE", "UNVERIFIED", "ALL"}:
            raise AppError("INVALID_FILTER", "不支持的候选筛选条件", status_code=422)

        ordered_concepts = [
            concept for concept in self._practice_concepts(project_id)
            if not source_id or concept.book_id == source_id
        ]
        concepts = {concept.concept_id: concept for concept in ordered_concepts}
        sequence = {concept.concept_id: index for index, concept in enumerate(ordered_concepts)}
        rows: list[dict] = []
        counts = {"questioned": 0, "weak": 0, "due": 0, "unverified": 0}
        for view in project_state_views(self.repo, project_id, policy=ReviewPolicy()):
            concept = concepts.get(view.concept_id)
            if concept is None:
                continue
            questions = [item for item in view.evidence if item.evidence_type == "QUESTION"]
            question_count = len(questions)
            if question_count:
                counts["questioned"] += 1
            if view.group == "weak":
                counts["weak"] += 1
            if view.group == "due":
                counts["due"] += 1
            if view.current_verified_level == "L0":
                counts["unverified"] += 1

            if view.group == "weak":
                priority, reason_code, reason_label = 0, "WEAK", "测验中还不稳"
            elif view.group == "due":
                priority, reason_code, reason_label = 1, "DUE", "已经到期，建议复验"
            elif view.current_verified_level in {"L1", "L2", "L3"}:
                next_level = f"L{int(view.current_verified_level[1:]) + 1}"
                priority, reason_code, reason_label = 2, "VERIFIED", f"已通过 {view.current_verified_level}，继续确认 {next_level}"
            elif question_count and view.current_verified_level == "L0":
                priority, reason_code, reason_label = 3, "QUESTIONED", "你问过，但还未验证"
            elif view.current_verified_level == "L0":
                priority, reason_code, reason_label = 4, "UNVERIFIED", "已进入学习范围，尚未验证"
            elif question_count:
                priority, reason_code, reason_label = 5, "QUESTIONED", "你曾问过，可再次确认"
            else:
                priority, reason_code, reason_label = 6, "VERIFIED", "已验证，可抽查"

            include_for_filter = {
                "QUESTIONED": question_count > 0,
                "WEAK": view.group == "weak",
                "DUE": view.group == "due",
                "UNVERIFIED": view.current_verified_level == "L0",
            }
            if selected_filter not in {"ALL", "RECOMMENDED"} and not include_for_filter[selected_filter]:
                continue
            source = self.repo.get_source(concept.book_id)
            pages = sorted({ref.physical_page for ref in concept.source_refs if ref.physical_page})
            locator = (
                f"第 {pages[0]} 页" if len(pages) == 1
                else f"第 {pages[0]}–{pages[-1]} 页" if pages
                else concept.section or concept.chapter or "相关内容"
            )
            rows.append({
                "concept_id": concept.concept_id,
                "name": concept.name,
                "reason_code": reason_code,
                "reason_label": reason_label,
                "current_state": view.group,
                "current_level": view.current_verified_level,
                "source_id": concept.book_id,
                "source_title": source.title if source else "学习资料",
                "locator": locator,
                "question_count": question_count,
                "last_question_at": questions[0].occurred_at if questions else None,
                "priority": priority,
                "sequence": sequence.get(concept.concept_id, len(sequence)),
            })
        rows.sort(key=lambda item: (item["priority"], -(item["question_count"] or 0), item["sequence"]))
        for item in rows:
            item.pop("sequence", None)
        return {
            "mode": mode,
            "filter": selected_filter,
            "counts": counts,
            "candidates": rows,
        }

    def get_task(self, task_id: str) -> dict | None:
        """Return a browser-safe view of a task (no rubric / concept ids)."""
        t = self.repo.get_trusted_task(task_id)
        if t is None:
            return None
        return {
            "task_id": t["task_id"],
            "prompt_text": t["prompt_text"],
            "kind": _kind_of(t),
            "is_probe": t["is_probe"],
            "is_changed_task": t["is_changed_task"],
            "remediation_stage": t["remediation_stage"],
            "hints_issued": t["hints_issued"],
            "status": t["status"],
        }

    def source_scope_for_task(self, task_id: str) -> list[dict]:
        """Return safe source locations for post-judgment navigation."""
        task = self.repo.get_trusted_task(task_id)
        if task is None:
            return []
        project_id = task["project_id"]
        target_ids = set(task.get("target_concept_ids") or [])
        items: list[dict] = []
        seen: set[str] = set()
        for concept in self._all_project_concepts(project_id):
            if concept.concept_id not in target_ids or concept.book_id in seen:
                continue
            seen.add(concept.book_id)
            source = self.repo.get_source(concept.book_id)
            page = next((ref.physical_page for ref in concept.source_refs if ref.physical_page), 0)
            if not page:
                for evidence in sorted(
                    self.repo.evidence_for(project_id, concept.concept_id),
                    key=lambda item: item.occurred_at,
                    reverse=True,
                ):
                    chunk = next(
                        (self.repo.chunk_by_id(chunk_id) for chunk_id in evidence.source_chunk_ids
                         if self.repo.chunk_by_id(chunk_id) is not None),
                        None,
                    )
                    if chunk is not None:
                        page = chunk.source_ref.physical_page
                        break
            page = page or 1
            items.append({
                "source_id": concept.book_id,
                "title": source.title if source else "学习资料",
                "page": page,
                "locator": f"第 {page} 页" if page else concept.section or concept.chapter or "相关内容",
            })
        return items

    # --- hints ------------------------------------------------------------

    def request_hint(self, task_id: str) -> dict:
        t = self.repo.get_trusted_task(task_id)
        if t is None:
            raise AppError("TASK_NOT_FOUND", "任务不存在", status_code=404)
        if t["status"] != "PENDING":
            raise AppError("TASK_NOT_PENDING", "该任务已结束，无法再请求提示",
                           status_code=409)
        n = self.repo.increment_task_hints(task_id)
        rubric = list(t.get("rubric") or [])
        # Hints are an interaction control and must return immediately. A live
        # model call here used to block for up to a minute, inviting repeated
        # clicks and making the whole card look broken. Deterministic layered
        # hints are grounded in the trusted rubric and never reveal the answer.
        hint_text = ""
        idx = min(n - 1, len(rubric) - 1) if rubric else -1
        if not hint_text and idx >= 0:
            lead = (
                "先把题目中的输入、需要跟踪的关键量和目标结果分别写出来"
                if n == 1 else
                "按关键操作发生的先后顺序手工走一遍最小示例，并在每一步记录状态变化"
                if n == 2 else
                "检查你的结论在空输入、最小规模或极端结构下是否仍成立"
            )
            hint_text = f"提示 {n}：{lead}。完成后重点核对：{rubric[idx]}。"
        elif not hint_text:
            hint_text = f"提示 {n}：先构造一个最小输入，逐步写出每次操作前后的状态，再检查边界情况。"
        return {"hint_text": hint_text, "hint_notice": HINT_NOTICE, "hints_issued": n}

    # --- skip -------------------------------------------------------------

    def skip_task(self, task_id: str) -> dict:
        t = self.repo.get_trusted_task(task_id)
        if t is None:
            raise AppError("TASK_NOT_FOUND", "任务不存在", status_code=404)
        if t["status"] != "PENDING":
            raise AppError("TASK_NOT_PENDING", "该任务已经结束", status_code=409)
        self.repo.update_task_status(task_id, "SKIPPED")
        return {"task_id": task_id, "status": "SKIPPED"}

    def explain_task(self, task_id: str) -> dict:
        """Build a task-specific explanation from the trusted task context.

        This deliberately does *not* call the general textbook Q&A flow.  A
        completed question already has a trusted prompt, answer and rubric;
        rerunning broad retrieval for it can select a table of contents or an
        unrelated page, producing an excerpt instead of an explanation.

        Revealing an explanation while a task is pending ends that attempt as
        ``EXPLAINED``.  The learner receives the answer but no answer Evidence
        is written, so the next task remains a clean, independent attempt.
        """
        task = self.repo.get_trusted_task(task_id)
        if task is None:
            raise AppError("TASK_NOT_FOUND", "任务不存在", status_code=404)
        was_pending = task["status"] == "PENDING"
        if was_pending:
            self.repo.update_task_status(task_id, "EXPLAINED")

        prompt = _bounded_text(str(task.get("prompt_text") or ""), 1800)
        expected = _bounded_text(str(task.get("expected_answer") or ""), 1800)
        rubric = [_bounded_text(str(item), 320) for item in (task.get("rubric") or [])]
        fallback = _local_task_explanation(prompt, expected, rubric)
        explanation = fallback

        if getattr(self.router.cfg, "live", False):
            system = (
                "你是严谨、耐心的中文学习教练。根据给出的题目、标准答案和判分要点，"
                "写一份能帮助学生真正学会的讲解。总长度不超过 650 个中文字符，必须在篇幅内完整结束；"
                "先给结论，再按推理步骤展开，并指出一个常见误区；"
                "不要引用或编造教材原文、页码和来源，不要谈论模型或评分系统。"
                "可以使用 Markdown 的标题、列表、加粗和代码块；只输出讲解正文。"
            )
            user = (
                f"题目：\n{prompt}\n\n"
                f"标准答案：\n{expected}\n\n"
                "判分要点：\n" + "\n".join(f"- {item}" for item in rubric)
            )
            result = self.router.complete(
                "task_explanation",
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.25,
                max_tokens=700,
            )
            candidate = (result.content or "").strip() if result.ok else ""
            if len(candidate) >= 40:
                explanation = candidate

        return {
            "task_id": task_id,
            "text": explanation,
            "revealed_while_pending": was_pending,
            # This is the state of the *completion card*, rather than a
            # rewrite of the original assessment result.  A learner who opens
            # an explanation after answering or skipping has still viewed an
            # explanation, and should get the same unambiguous terminal UI.
            "completion_status": "EXPLAINED",
            "source_scope": self.source_scope_for_task(task_id),
        }

    def random_unverified_concept(self, project_id: str):
        """Choose a fresh practice target among concepts not yet at L4.

        The primary practice button intentionally uses random selection.  The
        result-card “下一题” remains a same-concept continuation so L1--L4
        evidence can still accumulate coherently.
        """
        import random
        from ..services.learner_state_view import project_state_views
        states = {item.concept_id: item for item in project_state_views(self.repo, project_id, policy=ReviewPolicy())}
        choices = [
            concept for concept in self._practice_concepts(project_id)
            if states.get(concept.concept_id) is not None
            and states[concept.concept_id].current_verified_level != "L4"
        ]
        return random.SystemRandom().choice(choices) if choices else None

    def answer_followup(self, task_id: str, question: str) -> str:
        """Answer a post-task question without submitting any new evidence.

        This deliberately uses only the task's trusted context.  It is a
        read-only tutor turn: no QUESTION signal, Evidence, diagnosis or
        mastery state is written here.
        """
        task = self.repo.get_trusted_task(task_id)
        if task is None:
            raise AppError("TASK_NOT_FOUND", "题目不存在", status_code=404)
        if task.get("status") == "PENDING":
            raise AppError("TASK_STILL_PENDING", "请先作答、跳过或查看讲解后再追问。", status_code=409)
        if not task.get("followup_open"):
            raise AppError("FOLLOWUP_NOT_OPEN", "请先点击“追问本题”开始追问。", status_code=409)
        prompt = _bounded_text(task.get("prompt_text", ""), 1400)
        expected = _bounded_text(task.get("expected_answer", ""), 1400)
        rubric = "\n".join(f"- {_bounded_text(item, 240)}" for item in (task.get("rubric") or [])[:5])
        system = (
            "你是题后辅导老师。依据给出的题目、参考答案和评分点回答学生追问。"
            "解释推理，不要把追问当作新测验，不要评价或改写学生的掌握状态。"
            "若上下文无法支持，明确说明缺少哪一部分。输出简洁 Markdown，最多 650 个中文字符。"
        )
        result = self.router.complete(
            "task_followup",
            [{"role": "system", "content": system}, {"role": "user", "content": (
                f"题目：{prompt}\n\n参考答案：{expected}\n\n评分点：\n{rubric}\n\n学生追问：{question[:1600]}"
            )}],
            temperature=0.2, max_tokens=850,
        )
        if result.ok and result.content:
            return result.content.strip()
        return (
            "这是一条题后追问，不会影响学习状态。\n\n"
            f"本题应围绕以下结论理解：{expected or '请结合题干逐项核对评分点。'}\n\n"
            f"可先对照评分点：\n{rubric or '- 题干中的条件、过程与结论。'}"
        )

    def start_followup(self, task_id: str) -> dict:
        """Enter the persisted, read-only follow-up phase for one terminal task."""
        task = self.repo.get_trusted_task(task_id)
        if task is None:
            raise AppError("TASK_NOT_FOUND", "题目不存在", status_code=404)
        if task.get("status") == "PENDING":
            raise AppError("TASK_STILL_PENDING", "请先作答、跳过或查看讲解后再追问。", status_code=409)
        active = self.repo.active_followup_task_for_conversation(task["conversation_id"])
        if active is not None and active["task_id"] != task_id:
            raise AppError("FOLLOWUP_ACTIVE", "请先结束正在进行的题后追问。", status_code=409, action="END_FOLLOWUP")
        self.repo.set_task_followup_open(task_id, True)
        return {"task_id": task_id, "followup_open": True}

    def end_followup(self, task_id: str) -> dict:
        task = self.repo.get_trusted_task(task_id)
        if task is None:
            raise AppError("TASK_NOT_FOUND", "题目不存在", status_code=404)
        self.repo.set_task_followup_open(task_id, False)
        return {"task_id": task_id, "followup_open": False}

    # --- answer submission ------------------------------------------------

    def submit_answer(
        self,
        *,
        task_id: str,
        answer_text: str,
        idempotency_key: str,
        learner_id: str,
        run_id: str = "",
    ) -> dict:
        """Judge one answer and commit it through the Learning Engine.

        The browser sends only task_id + answer_text (+ idempotency_key). The
        trusted task is reconstructed from storage; the Diagnostician judges;
        the Engine writes Evidence and state.
        """
        t = self.repo.get_trusted_task(task_id)
        if t is None:
            raise AppError("TASK_NOT_FOUND", "任务不存在", status_code=404)
        project_id = t["project_id"]
        # Scope: the task's project must belong to this learner.
        self.repo.assert_project_owned_by(project_id, learner_id)

        # Idempotency: a repeated (task_id, idempotency_key) returns the stored
        # result without re-judging or re-writing Evidence.
        existing = self.repo.get_submission_by_idem(task_id, idempotency_key)
        if existing is not None:
            return _answer_result_from_stored(t, existing)

        clarification = _clarification_for(answer_text)
        if clarification:
            return {
                "task_id": task_id,
                "judgment": {
                    "judgment_status": JudgmentStatus.NEEDS_REVIEW.value,
                    "result": None,
                    "reason": "回答信息不足",
                    "criterion_results": [],
                },
                "evidence_id": None,
                "written": False,
                "needs_review": True,
                "clarification": clarification,
                "state_delta": {"mastery_transitions": [], "misconception_transitions": []},
                "next_action": None,
            }

        trusted = self._trusted_from_stored(t)
        # The interaction mode comes from the project's default_mode (M6) so the
        # Evidence records the mode the user is actually in, not a hardcoded one.
        preset = self.repo.get_project_mode(project_id) or UIPreset.DEEP_LEARNING
        activity_mode, intervention_policy = dimensions_for(preset)
        interaction = InteractionContext(
            activity_mode=activity_mode,
            intervention_policy=intervention_policy,
            ui_preset=preset,
            hints_issued=int(t.get("hints_issued", 0)),
            tools_exposed=[],
            answer_submitted_at=_now(),
        )

        judgment = self.diagnostician.judge(
            task=trusted,
            answer_text=answer_text,
            rubric=trusted.rubric,
            prompt_text=str(t.get("prompt_text") or ""),
            expected_answer=str(t.get("expected_answer") or ""),
        )
        judgment = self._attach_probe_signals(trusted, judgment, answer_text)

        submission_id = f"sub_{uuid.uuid4().hex[:12]}"
        evidence_id = f"e{uuid.uuid4().hex[:10]}"
        allowed = self.repo.allowed_book_ids(project_id)
        if not allowed:
            raise AppError("NO_BOOK", "学习空间尚未关联资料，无法记录学习证据",
                           status_code=409)
        source_book_id = next(iter(allowed))

        # Evidence, learning state, submission audit, and task lifecycle share
        # one outer transaction. The Engine's transaction nests into this one
        # on SQL repositories, eliminating a half-committed answer state.
        with self.repo.transaction():
            result = engine_submit_answer(
                self.repo, learner_id=learner_id, project_id=project_id, task=trusted,
                interaction=interaction, judgment=judgment, answer_text=answer_text,
                policy=ReviewPolicy(), submission_id=submission_id,
                evidence_id=evidence_id, source_book_id=source_book_id,
            )

            self.repo.save_submission({
                "submission_id": submission_id, "task_id": task_id,
                "project_id": project_id, "learner_id": learner_id,
                "answer_text": answer_text,
                "judgment": judgment.model_dump(mode="json"),
                "run_id": run_id, "idempotency_key": idempotency_key,
                "created_at": _now(),
            })
            # An uncertain judgment is not a completed attempt. Keep the task
            # pending so the learner can clarify without losing the question.
            if not result.needs_review:
                self.repo.update_task_status(task_id, "ANSWERED", last_submission_id=submission_id)

        return self._answer_result(task_id, judgment, result, project_id)

    # --- internals --------------------------------------------------------

    def _next_action(self, project_id: str, *, user_requested: bool = False) -> dict:
        views: list[ConceptView] = []
        for bid in self.repo.allowed_book_ids(project_id):
            for c in self.repo.concepts_for_book(bid):
                if not _is_practice_worthy(c):
                    continue
                s = self.repo.get_state(project_id, c.concept_id)
                views.append(ConceptView(concept=c, state=s))
        mis = self.repo.all_misconceptions(project_id)
        # The decision respects the project's mode (M6): QUIET_READING won't
        # proactively surface tasks, ASSESSMENT restricts to VERIFY/WAIT. But a
        # user who *asks* ("考考我") is never blocked by QUIET — passing
        # user_requested unlocks the proactive rules against the user-allowed
        # action set so the highest-value task (REMEDIATE/DIAGNOSE/VERIFY)
        # surfaces (action_matrix §10).
        preset = self.repo.get_project_mode(project_id) or UIPreset.QUIET_READING
        activity_mode, intervention_policy = dimensions_for(preset)
        inp = DecisionInput(
            activity_mode=activity_mode,
            intervention_policy=intervention_policy,
            ui_preset=preset.value,
            concepts=views, misconceptions=mis,
            user_requested=user_requested,
        )
        trace = decide(inp)
        return {
            "selected_action": trace.selected_action,
            "selected_concept_id": trace.selected_concept_id,
            "reason": trace.reason,
        }

    def _draft_for_action(
        self, project_id: str, action: str | None, concept_id: str,
        *, strict_concept: bool = False,
    ) -> TaskDraft | None:
        if strict_concept:
            concept = self._exact_concept(project_id, concept_id)
            if concept is None:
                return None
            return generate_quiz(
                concept=concept, level=self._next_quiz_level(project_id, concept.concept_id),
                target_concept_ids=[concept.concept_id],
                router=self.router,
                source_context=self._source_context_for_concept(project_id, concept),
            )
        mis = self.repo.all_misconceptions(project_id)

        if action == Action.DIAGNOSE.value:
            bug = self._bug_for_concept(mis, concept_id)
            if bug is not None:
                targets = self._resolve_targets(bug.related_concepts, concept_id, project_id)
                return generate_probe(bug, target_concept_ids=targets, level=Level.L2,
                                       router=self.router)

        if action == Action.REMEDIATE.value:
            bug = self._bug_for_concept(mis, concept_id, any_status=(
                "CONFIRMED", "REMEDIATING", "VERIFYING"))
            if bug is not None:
                return self._remediation_draft(project_id, bug, mis, concept_id)

        if action == Action.VERIFY.value:
            bug = self._bug_for_concept(mis, concept_id, any_status=("VERIFYING",))
            if bug is not None:
                return self._remediation_draft(project_id, bug, mis, concept_id)

        # Active-but-unconfirmed misconception → prefer a diagnostic probe over a
        # generic quiz. CONFIRMED is handled by REMEDIATE above; here we advance
        # SUSPECTED/LIKELY toward confirmation by emitting the high-discrimination
        # probe evidence CONFIRMED requires (state_machine: ≥1 probe evidence).
        # This is an out-creation choice, not a state write — the Engine remains
        # the only state authority. (PRODUCTIZATION §M4: probe reachable in the
        # product path; single-bug probes discriminate the bug's own competing
        # hypotheses, LEARNING_MODEL §9.) The Engine often selects WAIT here
        # (no positive-value action for an unconfirmed misconception), but the
        # user asked to be quizzed, so we surface the most diagnostic task.
        # A RESOLVED bug is also probed: a new high-disc FOR evidence triggers
        # RELAPSED (LEARNING_MODEL §8: "RESOLVED + new high-disc support →
        # RELAPSED"), exercising the recurrence path.
        if action in (Action.VERIFY.value, Action.REVIEW.value,
                      Action.LEARN_PREREQUISITE.value, Action.WAIT.value, None):
            active = [m for m in mis
                      if m.status.value in ("SUSPECTED", "LIKELY", "RESOLVED")
                      and not _has_active_competing(m, mis)]
            if active:
                bug = BUG_LIBRARY.get(active[0].bug_id)
                if bug is not None:
                    targets = self._resolve_targets(bug.related_concepts, concept_id, project_id)
                    return generate_probe(bug, target_concept_ids=targets, level=Level.L2,
                                           router=self.router)

        # REVIEW / LEARN_PREREQUISITE / default → an ordinary quiz on the
        # selected concept. Fall back to any in-scope concept if none selected.
        concept = self._concept_in_scope(project_id, concept_id)
        if concept is None:
            return None
        return generate_quiz(
            concept=concept, level=self._next_quiz_level(project_id, concept.concept_id),
            target_concept_ids=[concept.concept_id],
            router=self.router,
            source_context=self._source_context_for_concept(project_id, concept),
        )

    def _next_quiz_level(self, project_id: str, concept_id: str) -> Level:
        """Return the first mastery level not yet continuously verified.

        A fresh concept used to receive an L2 question.  Even a perfect answer
        then left the visible level at L0 because L1 was still missing.  Task
        levels must follow the same L1→L4 ladder that the Evidence Gate uses.
        """
        current = self.repo.get_state(project_id, concept_id).current_verified_level
        ladder = [Level.L1, Level.L2, Level.L3, Level.L4]
        try:
            index = ladder.index(current) + 1
        except ValueError:
            index = 0
        return ladder[min(index, len(ladder) - 1)]

    def next_practice_concept_after(self, project_id: str, concept_id: str) -> str:
        """Choose the next unmastered sibling after a fully verified concept."""
        concepts = self._practice_concepts(project_id)
        current = next((i for i, concept in enumerate(concepts) if concept.concept_id == concept_id), -1)
        if current < 0:
            return ""
        current_book = concepts[current].book_id
        # Prefer the next section of the same source, preserving the mapper's
        # source order.  Do not wrap to the first chapter after completing L4.
        for concept in concepts[current + 1:]:
            if concept.book_id != current_book:
                continue
            if self.repo.get_state(project_id, concept.concept_id).current_verified_level != Level.L4:
                return concept.concept_id
        return ""

    def _source_context_for_concept(self, project_id: str, concept) -> str:
        """Pick compact source passages anchored to a concept for quiz writing."""
        refs = list(getattr(concept, "source_refs", None) or [])
        ref_chunk_ids = {ref.chunk_id for ref in refs if ref.chunk_id}
        ref_pages = {
            (ref.document_id, ref.physical_page)
            for ref in refs if ref.physical_page is not None
        }
        ranked: list[tuple[int, str]] = []
        for chunk in self.repo.chunks_for_project(project_id):
            score = 0
            if chunk.chunk_id in ref_chunk_ids:
                score += 10
            if (chunk.document_id, chunk.source_ref.physical_page) in ref_pages:
                score += 6
            if concept.name.casefold() in chunk.content.casefold():
                score += 3
            if score:
                ranked.append((score, chunk.content.strip()))
        ranked.sort(key=lambda item: (-item[0], len(item[1])))
        passages: list[str] = []
        total = 0
        for _, content in ranked:
            if not content or content in passages:
                continue
            excerpt = content[:2200]
            if total + len(excerpt) > 6000:
                excerpt = excerpt[: max(0, 6000 - total)]
            if excerpt:
                passages.append(excerpt)
                total += len(excerpt)
            if len(passages) >= 3 or total >= 6000:
                break
        if passages:
            return "\n\n".join(passages)
        return (getattr(concept, "description", "") or "").strip()

    def _model_hint(self, task: dict, hint_number: int) -> str:
        """Ask the live model for a tailored Socratic hint, never the answer."""
        if not getattr(self.router.cfg, "live", False):
            return ""
        rubric = "\n".join(f"- {item}" for item in (task.get("rubric") or []))
        system = (
            "你是苏格拉底式学习教练。请针对题目给一条具体、可执行的中文提示，引导学生完成下一步推理。"
            "不能直接给出最终答案、完整步骤、代码或结论；不能只说‘想一想某概念’。"
            "第一条提示帮助拆解输入和目标，第二条提示引导手工模拟关键步骤，后续提示引导检查边界。"
            "只输出提示正文，不要标题和解释。"
        )
        user = (
            f"题目：{task.get('prompt_text', '')}\n"
            f"这是第 {hint_number} 条提示。\n"
            f"评分关注点：\n{rubric}\n"
            f"参考答案仅用于控制泄露程度，不得复述：{task.get('expected_answer', '')}"
        )
        result = self.router.complete(
            "socratic_task_hint",
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.4,
            max_tokens=2048,
        )
        if not result.ok or not result.content:
            return ""
        text = result.content.strip().strip('"')
        if len(text) < 12 or len(text) > 360:
            return ""
        return f"提示 {hint_number}：{text}"

    def _remediation_draft(
        self, project_id: str, bug: BugEntry, mis: list, concept_id: str,
    ) -> TaskDraft | None:
        svc = RemediationService(self.repo, self.router)
        # Find the misconception row to decide near vs far transfer.
        row = next((m for m in mis if m.bug_id == bug.bug_id), None)
        stage = 2 if row is not None and row.status.value == "VERIFYING" else 1
        # If remediation hasn't started yet for a CONFIRMED bug, start it first
        # (CONFIRMED → REMEDIATING). start() is idempotent-ish: it refuses if
        # not CONFIRMED, so guard it.
        if row is not None and row.status.value == "CONFIRMED":
            try:
                svc.start(project_id=project_id, bug_id=bug.bug_id)
            except Exception:
                pass  # already REMEDIATING or in transition; build the task anyway
        targets = self._resolve_targets(bug.related_concepts, concept_id, project_id)
        res = svc.build_changed_task(
            project_id=project_id, bug_id=bug.bug_id, stage=stage,
            target_concept_ids=targets, level=Level.L3,
        )
        # build_changed_task validates internally; if it failed, fall back to a
        # raw generate so the orchestrator still has a draft to try.
        if res.report.passed and res.trusted is not None:
            # Reconstruct a TaskDraft mirroring the trusted context + prompt.
            return _draft_from_trusted(res.trusted, _changed_prompt(bug, stage))
        return generate_changed_task(bug, stage=stage, target_concept_ids=targets,
                                     level=Level.L3, router=self.router)

    def _bug_for_concept(
        self, mis: list, concept_id: str, *, any_status: tuple[str, ...] | None = None,
    ) -> BugEntry | None:
        """Pick a bug whose related concepts include the decision's concept, or
        any active misconception's bug. Active = not DISMISSED/RESOLVED unless a
        specific status set is requested."""
        target_statuses = any_status or ("SUSPECTED", "LIKELY", "RELAPSED")
        # Prefer a misconception tied to the selected concept.
        for m in mis:
            if m.status.value in ("DISMISSED", "RESOLVED"):
                continue
            if any_status is None and m.status.value not in target_statuses:
                continue
            if concept_id and concept_id in (m.related_concepts or []):
                bug = BUG_LIBRARY.get(m.bug_id)
                if bug is not None:
                    return bug
        # Otherwise any active misconception for the project.
        for m in mis:
            if any_status is None and m.status.value not in target_statuses:
                continue
            if any_status and m.status.value not in any_status:
                continue
            bug = BUG_LIBRARY.get(m.bug_id)
            if bug is not None:
                return bug
        return None

    def _resolve_targets(
        self, bug_concepts: list[str], fallback_concept_id: str, project_id: str,
    ) -> list[str]:
        """Pick target_concept_ids that are in project scope.

        Bug related_concepts use golden skeleton IDs (c_reference, ...) which
        are in scope for the seeded Java demo but may not be for a real uploaded
        book. Resolution order: (1) in-scope bug concepts; (2) name match
        against project concepts; (3) the decision engine's selected concept.
        """
        in_scope = [c for c in bug_concepts if self.repo.concept_in_project_scope(c, project_id)]
        if in_scope:
            return in_scope
        # Name-based fallback: map golden id → name, find a project concept with
        # a matching name (case-insensitive substring).
        name_map = _skeleton_name_map()
        project_concepts = self._all_project_concepts(project_id)
        for cid in bug_concepts:
            name = name_map.get(cid, "")
            if not name:
                continue
            for c in project_concepts:
                if name and (name.lower() in c.name.lower() or c.name.lower() in name.lower()):
                    return [c.concept_id]
        if fallback_concept_id and self.repo.concept_in_project_scope(fallback_concept_id, project_id):
            return [fallback_concept_id]
        # Last resort: any in-scope concept.
        return [c.concept_id for c in project_concepts[:1]]

    def _concept_in_scope(self, project_id: str, concept_id: str):
        concepts = self._practice_concepts(project_id)
        for concept in concepts:
            if concept.concept_id == concept_id:
                return concept
        # Fall back to an actual learning section, never the cover, preface,
        # table of contents, appendix index, or a chapter-only heading.
        if concepts:
            return concepts[0]
        return None

    def _exact_concept(self, project_id: str, concept_id: str):
        """Resolve an explicit UI selection without silently changing target."""
        for concept in self._all_project_concepts(project_id):
            if concept.concept_id == concept_id:
                return concept
        return None

    def _all_project_concepts(self, project_id: str):
        out = []
        for bid in self.repo.allowed_book_ids(project_id):
            out.extend(self.repo.concepts_for_book(bid))
        return out

    def _practice_concepts(self, project_id: str):
        return [
            concept for concept in self._all_project_concepts(project_id)
            if _is_practice_worthy(concept)
        ]

    def _preferred_practice_concept(self, project_id: str, user_text: str):
        """Resolve a learner-selected or previously-questioned task target."""
        text = user_text.casefold()
        concepts = self._practice_concepts(project_id)

        # A concept clicked in the UI is placed in the prompt by name.
        named = [concept for concept in concepts if concept.name.casefold() in text]
        if named:
            return max(named, key=lambda concept: (len(concept.name), concept.importance))

        # Generic requests such as “从我问过的知识点出题” use the most recent
        # QUESTION evidence. Asking never marks the concept weak; it only gives
        # this queue a candidate to verify.
        if any(cue in user_text for cue in ("问过", "疑问", "不懂", "没弄懂")):
            candidates = []
            for concept in concepts:
                questions = [
                    evidence for evidence in self.repo.evidence_for(project_id, concept.concept_id)
                    if evidence.evidence_type.value == "QUESTION"
                ]
                if questions:
                    candidates.append((max(item.occurred_at for item in questions), concept))
            if candidates:
                return max(candidates, key=lambda item: (item[0], item[1].importance))[1]
        return None

    def _attach_probe_signals(
        self, trusted: TrustedTaskContext, judgment: AnswerJudgment, answer_text: str,
    ) -> AnswerJudgment:
        """If this is a probe and the Diagnostician emitted no signals, classify
        deterministically/online so the competing-hypotheses invariant holds.
        Also drop any signal whose bug_id is not in discriminated_bug_ids."""
        signals = list(judgment.misconception_signals)
        allowed_bugs = set(trusted.discriminated_bug_ids or [])
        if trusted.is_probe and trusted.discriminated_bug_ids and not signals:
            bug = BUG_LIBRARY.get(trusted.discriminated_bug_ids[0])
            if bug is not None:
                signals = signals_for_probe(bug, answer_text, router=self.router)
        if allowed_bugs:
            signals = [s for s in signals if s.bug_id in allowed_bugs]
        if signals != judgment.misconception_signals:
            return judgment.model_copy(update={"misconception_signals": signals})
        return judgment

    def _persist_task(
        self, trusted: TrustedTaskContext, draft: TaskDraft, *,
        project_id: str, learner_id: str, conversation_id: str,
    ) -> None:
        self.repo.save_trusted_task({
            "task_id": trusted.task_id, "project_id": project_id,
            "conversation_id": conversation_id, "run_id": "",
            "learner_id": learner_id,
            "task_version": trusted.task_version,
            "target_concept_ids": list(trusted.target_concept_ids),
            "evidence_for_levels": [l.value for l in trusted.evidence_for_levels],
            "rubric": list(trusted.rubric),
            "allowed_resources": list(trusted.allowed_resources),
            "source_refs": [r.model_dump(mode="json") for r in trusted.source_refs],
            "scenario_fingerprint": trusted.scenario_fingerprint,
            "is_probe": trusted.is_probe,
            "discriminated_bug_ids": list(trusted.discriminated_bug_ids),
            "is_changed_task": trusted.is_changed_task,
            "remediation_stage": trusted.remediation_stage,
            "prompt_text": draft.prompt_text,
            "expected_answer": draft.expected_answer,
            "distractors": list(draft.distractors),
            "status": "PENDING", "hints_issued": 0,
            "last_submission_id": None,
            "created_at": _now(), "expires_at": None,
        })

    def _trusted_from_stored(self, t: dict) -> TrustedTaskContext:
        from ..domain.source_ref import SourceRef
        return TrustedTaskContext(
            task_id=t["task_id"], task_version=t["task_version"],
            target_concept_ids=list(t["target_concept_ids"]),
            evidence_for_levels=[_level_from_str(s) for s in t["evidence_for_levels"]],
            rubric=list(t["rubric"]),
            allowed_resources=list(t.get("allowed_resources") or []),
            source_refs=[SourceRef(**r) for r in (t.get("source_refs") or [])],
            scenario_fingerprint=t.get("scenario_fingerprint"),
            is_probe=t["is_probe"],
            discriminated_bug_ids=list(t["discriminated_bug_ids"]),
            is_changed_task=t["is_changed_task"],
            remediation_stage=t["remediation_stage"],
        )

    def _card_payload(self, trusted: TrustedTaskContext, draft: TaskDraft,
                      decision: dict, project_id: str) -> dict:
        target_ids = set(trusted.target_concept_ids)
        target_concepts = [
            concept
            for source_id in self.repo.allowed_book_ids(project_id)
            for concept in self.repo.concepts_for_book(source_id)
            if concept.concept_id in target_ids
        ]
        source_items: list[dict] = []
        seen_sources: set[str] = set()
        for concept in target_concepts:
            if concept.book_id in seen_sources:
                continue
            seen_sources.add(concept.book_id)
            source = self.repo.get_source(concept.book_id)
            pages = sorted({
                ref.physical_page for item in target_concepts
                if item.book_id == concept.book_id for ref in item.source_refs
            })
            source_items.append({
                "source_id": concept.book_id,
                "title": source.title if source else "学习资料",
                "locator": (
                    f"第 {pages[0]} 页" if len(pages) == 1
                    else f"第 {pages[0]}–{pages[-1]} 页" if pages
                    else concept.section or concept.chapter or "相关内容"
                ),
            })
        action = decision.get("selected_action") or "VERIFY"
        target_level = trusted.evidence_for_levels[0] if trusted.evidence_for_levels else Level.L1
        reason_map = {
            "VERIFY": {
                Level.L1: "这是该知识点的第一次独立验证；答对后会建立 L1 基础掌握记录。",
                Level.L2: "你已通过 L1；这题用于确认能否解释并应用该知识点（L2）。",
                Level.L3: "你已通过 L2；这题要求在新情境中迁移使用该知识点（L3）。",
                Level.L4: "你已通过 L3；这题用于检验能否独立综合运用并巩固到 L4。",
            }.get(target_level, "这题用于确认当前知识点的独立掌握情况。"),
            "REVIEW": "这个知识点需要复习，当前问题用于检查是否仍能独立回忆。",
            "DIAGNOSE": "之前的回答可能存在理解偏差，这个问题用于区分具体原因。",
            "REMEDIATE": "这个问题用于针对已发现的理解偏差进行纠正。",
            "LEARN_PREREQUISITE": "继续当前内容前，需要先确认一个相关的基础知识点。",
        }
        return {
            "kind": _kind_of_trusted(trusted),
            "task_id": trusted.task_id,
            "prompt_text": draft.prompt_text,
            "is_probe": trusted.is_probe,
            "is_changed_task": trusted.is_changed_task,
            "remediation_stage": trusted.remediation_stage,
            "next_action": decision,
            "focus": "、".join(c.name for c in target_concepts[:2]) or "当前知识点",
            "source_scope": source_items,
            "generation_reason": reason_map.get(
                action, "根据当前学习进度生成，用于确认你是否真正理解了这部分资料。",
            ),
            "generation_mode": draft.generation_mode,
            "generation_notice": draft.generation_notice,
        }

    def _answer_result(
        self, task_id: str, judgment: AnswerJudgment, res: Any, project_id: str,
    ) -> dict:
        next_action = None if res.needs_review else self._next_action(project_id)
        judgment_view = _judgment_view(judgment)
        clarification = ""
        if res.needs_review:
            timeout_notice = ""
            if "timeout" in (judgment.reason or "").casefold():
                timeout_notice = "判分模型服务响应超时；本地规则也无法可靠覆盖这份答案。"
            judgment_view = {
                "judgment_status": JudgmentStatus.NEEDS_REVIEW.value,
                "result": None,
                "reason": timeout_notice or "这段回答还不足以判断你是否理解了题目。",
                "criterion_results": [],
            }
            clarification = "请说明你的判断和理由；如果不确定，可以说“我不知道”或先跳过这道题。"
        return {
            "task_id": task_id,
            "judgment": judgment_view,
            "evidence_id": res.evidence_id,
            "written": res.written,
            "needs_review": res.needs_review,
            "clarification": clarification,
            "state_delta": {
                "mastery_transitions": [t.model_dump(mode="json") for t in res.mastery_transitions],
                "misconception_transitions": [t.model_dump(mode="json") for t in res.misconception_transitions],
            },
            "next_action": next_action,
        }


# --- helpers ----------------------------------------------------------------

_NON_LEARNING_SECTIONS = {
    "序", "丛书序", "前言", "第1版前言", "第2版说明", "第3版说明",
    "致谢", "简要目录", "详细目录", "教学计划编排方案建议",
    "参考文献", "算法索引", "代码索引", "关键词索引",
}


def _normalise_section_name(value: str) -> str:
    return "".join((value or "").split()).lstrip("§*")


def _is_practice_worthy(concept) -> bool:
    """Keep real textbook practice focused on teachable leaf sections."""
    if concept.book_id == "demo_java_core":
        return True
    name = (concept.name or "").strip()
    normalised_name = _normalise_section_name(name)
    excluded = {_normalise_section_name(item) for item in _NON_LEARNING_SECTIONS}
    if (
        not normalised_name
        or normalised_name in excluded
        or "目录" in normalised_name
        or normalised_name.endswith("索引")
        or _normalise_section_name(concept.chapter or "").startswith("附录")
    ):
        return False
    # A book/chapter heading can inherit references from all descendants. It
    # is practice-worthy only when it is itself the leaf of a real subsection.
    paths = [tuple(ref.section_path) for ref in concept.source_refs if ref.section_path]
    return any(
        len(path) >= 2
        and _normalise_section_name(path[-1]) == normalised_name
        for path in paths
    )


def _clarification_for(answer_text: str) -> str:
    """Return guidance when an input cannot reasonably be treated as an answer."""
    raw = (answer_text or "").strip()
    if not raw:
        return "我还没收到可以判断的答案。请写下你的结论和理由；如果不想回答，可以选择跳过。"
    # Do not reject concise but valid answers before the Diagnostician sees
    # them: `O(1)`, `是`, a symbol, or a multiple-choice option may be the
    # complete answer to a well-formed task.
    compact = "".join(ch for ch in raw if ch.isalnum())
    if compact.lower() in {"不知道", "不清楚", "不会", "idk", "test", "测试"}:
        return "没关系。你可以说说卡在哪一步，或者选择跳过；这次不会记录为错误答案。"
    return ""


def _bounded_text(value: str, limit: int) -> str:
    """Keep a malformed historical task from bloating an explanation prompt."""
    compact = " ".join((value or "").split())
    return compact[:limit].rstrip()


def _local_task_explanation(prompt: str, expected: str, rubric: list[str]) -> str:
    """Useful explanation when the live teacher call is unavailable.

    The trusted answer/rubric is much safer and more relevant than a broad
    retrieval fallback: it is exactly the material the completed task used for
    judging.  This is intentionally a real explanation structure, not a raw
    source excerpt.
    """
    answer = expected or "请根据下列要点逐项完成推理。"
    steps = "\n".join(f"{index}. {item}" for index, item in enumerate(rubric, start=1))
    return (
        "## 这题怎么想\n"
        "先把题目要求拆成可验证的结论，再分别说明每个结论成立的理由；不要只给最终结果。\n\n"
        f"## 参考结论\n{answer}\n\n"
        f"## 关键检查点\n{steps or '围绕题目中的条件、过程和结论逐步核对。'}\n\n"
        "## 常见误区\n"
        "只写一个结果、忽略边界条件，或把相近概念混为一谈，都会使答案缺少可验证的推理。"
    )

def _has_active_competing(mis, all_mis: list) -> bool:
    """True if another active misconception shares this one's hypothesis_group.

    A misconception with no hypothesis_group (the common product-path case) has
    no competitors, so its probe is free to advance toward confirmation.
    Mirrors the decision layer's competing-group notion without importing its
    private helper.
    """
    if mis.hypothesis_group is None:
        return False
    peers = [m for m in all_mis
             if m.hypothesis_group == mis.hypothesis_group and m.bug_id != mis.bug_id]
    return any(m.status.value not in ("DISMISSED", "RESOLVED") for m in peers)


def _kind_of(t: dict) -> str:
    if t.get("is_probe"):
        return "probe"
    if t.get("is_changed_task"):
        return "changed_task"
    return "quiz"


def _kind_of_trusted(t: TrustedTaskContext) -> str:
    if t.is_probe:
        return "probe"
    if t.is_changed_task:
        return "changed_task"
    return "quiz"


def _judgment_view(j: AnswerJudgment) -> dict:
    """Return a learner-facing judgment without exposing model/rubric internals."""
    public_reason = {
        EvidenceResult.PASS: "回答覆盖了本题需要的关键要点。",
        EvidenceResult.PARTIAL: "方向基本正确，但还有关键要点需要补充。",
        EvidenceResult.FAIL: "这次回答还没有体现出本题考查的关键理解。",
    }.get(j.result, "这段回答还不足以判断你是否理解了题目。")
    return {
        "judgment_status": j.judgment_status.value,
        "result": j.result.value if j.result is not None else None,
        "reason": public_reason,
        "criterion_results": [
            {"criterion_id": str(index), "satisfied": c.satisfied, "note": ""}
            for index, c in enumerate(j.criterion_results, start=1)
        ],
    }


def _answer_result_from_stored(task: dict, sub: dict) -> dict:
    """Reconstruct an AnswerResult from a stored submission (idempotent replay)."""
    judg = sub.get("judgment") or {}
    result = judg.get("result")
    public_reason = {
        EvidenceResult.PASS.value: "回答覆盖了本题需要的关键要点。",
        EvidenceResult.PARTIAL.value: "方向基本正确，但还有关键要点需要补充。",
        EvidenceResult.FAIL.value: "这次回答还没有体现出本题考查的关键理解。",
    }.get(result, "这段回答还不足以判断你是否理解了题目。")
    criteria = judg.get("criterion_results") or []
    return {
        "task_id": task["task_id"],
        "judgment": {
            "judgment_status": judg.get("judgment_status", "NEEDS_REVIEW"),
            "result": result,
            "reason": public_reason,
            "criterion_results": [
                {
                    "criterion_id": str(index),
                    "satisfied": bool(item.get("satisfied")),
                    "note": "",
                }
                for index, item in enumerate(criteria, start=1)
            ],
        },
        "evidence_id": None,
        "written": False,
        "needs_review": judg.get("judgment_status") == JudgmentStatus.NEEDS_REVIEW.value,
        "state_delta": {"mastery_transitions": [], "misconception_transitions": []},
        "next_action": None,
        "replay": True,
    }


def _changed_prompt(bug: BugEntry, stage: int) -> str:
    idx = 0 if stage == 1 else 1
    return bug.changed_task_templates[idx] if idx < len(bug.changed_task_templates) else ""


def _draft_from_trusted(trusted: TrustedTaskContext, prompt_text: str) -> TaskDraft:
    return TaskDraft(
        task_id=trusted.task_id, task_version=trusted.task_version,
        target_concept_ids=list(trusted.target_concept_ids),
        evidence_for_levels=list(trusted.evidence_for_levels),
        rubric=list(trusted.rubric),
        prompt_text=prompt_text,
        is_probe=trusted.is_probe,
        discriminated_bug_ids=list(trusted.discriminated_bug_ids),
        is_changed_task=trusted.is_changed_task,
        scenario_fingerprint=trusted.scenario_fingerprint,
        remediation_stage=trusted.remediation_stage,
    )


_SKELETON_NAME_CACHE: dict[str, str] | None = None


def _skeleton_name_map() -> dict[str, str]:
    global _SKELETON_NAME_CACHE
    if _SKELETON_NAME_CACHE is not None:
        return _SKELETON_NAME_CACHE
    from ..agents.concept_skeleton import _SKELETON
    _SKELETON_NAME_CACHE = {row[0]: row[1] for row in _SKELETON}
    return _SKELETON_NAME_CACHE
