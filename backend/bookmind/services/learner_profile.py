"""LLM-maintained learner profiles projected from immutable evidence.

The ledger records what happened. This module asks the LLM to explain the
pedagogical meaning of those facts in the vocabulary of the current textbook.
Profiles are replaceable projections; Evidence remains the audit source.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from pydantic import BaseModel, Field

from ..domain.enums import EvidenceType, HintLevel, MemoryKind
from ..domain.models import AnswerJudgment, Evidence, InteractionContext, LearningMemory, TrustedTaskContext, utcnow
from ..llm.router import ModelRouter
from ..storage.protocols import Repository


PROFILE_PROMPT_VERSION = "learner_profile_v1"
_MAX_FACTS = 10


class ConceptProfile(BaseModel):
    """Open-ended semantic interpretation for one textbook learning unit.

    The contents are authored by the model from the task and evidence. No
    course-specific skill taxonomy is hard-coded here.
    """

    concept_id: str
    summary: str = ""
    observed_understanding: list[str] = Field(default_factory=list)
    needs_attention: list[str] = Field(default_factory=list)
    next_practice_goal: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_basis: list[str] = Field(default_factory=list)


def profile_memory_id(project_id: str, concept_id: str) -> str:
    digest = hashlib.sha256(f"{project_id}|{concept_id}|learner-profile".encode("utf-8")).hexdigest()[:24]
    return f"mem_profile_{digest}"


def profile_for(repo: Repository, project_id: str, concept_id: str) -> dict[str, Any] | None:
    memory_id = profile_memory_id(project_id, concept_id)
    for item in repo.memories_for_project(
        project_id, kind=MemoryKind.LEARNER_PROFILE.value, concept_id=concept_id,
    ):
        if item.memory_id == memory_id:
            profile = item.metadata.get("profile")
            if isinstance(profile, dict):
                return dict(profile)
    return None


def profiles_for_project(repo: Repository, project_id: str) -> dict[str, dict[str, Any]]:
    """Load the profile projection once for list views (avoids N+1 reads)."""
    out: dict[str, dict[str, Any]] = {}
    for item in repo.memories_for_project(project_id, kind=MemoryKind.LEARNER_PROFILE.value):
        profile = item.metadata.get("profile")
        if item.concept_id and isinstance(profile, dict):
            out[item.concept_id] = dict(profile)
    return out


def render_profile_context(
    repo: Repository, project_id: str, concept_ids: list[str], *, limit: int = 3,
) -> str:
    """Return bounded non-evidentiary guidance for Tutor and task generation."""
    lines: list[str] = []
    for concept_id in concept_ids[:limit]:
        profile = profile_for(repo, project_id, concept_id)
        if not profile:
            continue
        summary = _short(profile.get("summary"), 220)
        goal = _short(profile.get("next_practice_goal"), 180)
        needs = _short_list(profile.get("needs_attention"), 2, 130)
        if summary or goal or needs:
            lines.append(
                f"{concept_id}：{summary or '已有作答记录，等待进一步解释。'}"
                + (f"；待关注：{needs}" if needs else "")
                + (f"；建议验证：{goal}" if goal else "")
            )
    return "\n".join(lines)


def record_task_interaction_fact(
    repo: Repository,
    *,
    task_data: dict[str, Any],
    evidence_type: EvidenceType,
    detail: str = "",
) -> list[str]:
    """Append non-verifying task facts for hints, skips and explanations."""
    if evidence_type not in {EvidenceType.HINT, EvidenceType.SKIP, EvidenceType.EXPLANATION}:
        raise ValueError(f"unsupported task interaction evidence type: {evidence_type}")
    project_id = str(task_data.get("project_id") or "")
    task_id = str(task_data.get("task_id") or "")
    if not project_id or not task_id:
        return []
    concepts = {
        concept.concept_id: concept
        for book_id in repo.allowed_book_ids(project_id)
        for concept in repo.concepts_for_book(book_id)
    }
    written: list[str] = []
    for concept_id in task_data.get("target_concept_ids") or []:
        concept = concepts.get(concept_id)
        if concept is None:
            continue
        marker = detail or "1"
        digest = hashlib.sha256(
            f"{project_id}|{task_id}|{concept_id}|{evidence_type.value}|{marker}".encode("utf-8"),
        ).hexdigest()[:20]
        evidence = Evidence(
            evidence_id=f"fact_{uuid.uuid4().hex[:12]}",
            event_key=f"task_fact_{digest}",
            project_id=project_id,
            concept_id=concept_id,
            source_book_id=concept.book_id,
            source_refs=list(concept.source_refs),
            evidence_type=evidence_type,
            hint_level=HintLevel.NONE,
            task_id=task_id,
            task_version=int(task_data.get("task_version") or 1),
            source_session=str(task_data.get("conversation_id") or ""),
            content_summary=f"task_interaction:{evidence_type.value}:{_short(detail, 180)}",
        )
        if repo.append_evidence(evidence):
            written.append(evidence.evidence_id)
    return written


def record_unresolved_submission_fact(
    repo: Repository,
    *,
    task_data: dict[str, Any],
    answer_text: str,
    hints_issued: int,
    reason: str,
    marker: str,
) -> list[str]:
    """Keep an attempted answer when no reliable PASS/PARTIAL/FAIL exists.

    The row uses VERIFY because the learner did submit to a real task, but its
    result is deliberately None. The evidence gate therefore cannot verify a
    level, and the display grouping does not turn this into a fabricated fail.
    """
    project_id = str(task_data.get("project_id") or "")
    task_id = str(task_data.get("task_id") or "")
    if not project_id or not task_id:
        return []
    concepts = {
        concept.concept_id: concept
        for book_id in repo.allowed_book_ids(project_id)
        for concept in repo.concepts_for_book(book_id)
    }
    written: list[str] = []
    for concept_id in task_data.get("target_concept_ids") or []:
        concept = concepts.get(concept_id)
        if concept is None:
            continue
        digest = hashlib.sha256(
            f"{project_id}|{task_id}|{concept_id}|unresolved|{marker}".encode("utf-8"),
        ).hexdigest()[:20]
        evidence = Evidence(
            evidence_id=f"attempt_{uuid.uuid4().hex[:12]}",
            event_key=f"unresolved_attempt_{digest}",
            project_id=project_id,
            concept_id=concept_id,
            source_book_id=concept.book_id,
            source_refs=list(concept.source_refs),
            evidence_type=EvidenceType.VERIFY,
            result=None,
            independent=hints_issued == 0,
            hint_level=HintLevel.NONE if hints_issued == 0 else HintLevel.LOW,
            task_id=task_id,
            task_version=int(task_data.get("task_version") or 1),
            source_session=str(task_data.get("conversation_id") or ""),
            content_summary=(
                f"unresolved_submission:{_short(reason, 150)};"
                f"answer_length={len((answer_text or '').strip())}"
            ),
        )
        if repo.append_evidence(evidence):
            written.append(evidence.evidence_id)
    return written


class LearnerProfileService:
    """Creates a replaceable semantic projection after a judged submission."""

    def __init__(self, repo: Repository, router: ModelRouter) -> None:
        self.repo = repo
        self.router = router

    def update_after_submission(
        self,
        *,
        project_id: str,
        task: TrustedTaskContext,
        interaction: InteractionContext,
        judgment: AnswerJudgment,
        answer_text: str,
        evidence: Evidence,
    ) -> list[dict[str, Any]]:
        targets = self._target_concepts(project_id, task.target_concept_ids)
        if not targets:
            return []
        result = self.router.complete(
            "learner_profile_update",
            self._messages(
                project_id=project_id,
                targets=targets,
                task=task,
                interaction=interaction,
                judgment=judgment,
                answer_text=answer_text,
                evidence=evidence,
            ),
            output_schema={"type": "object"}, temperature=0.1, max_tokens=900,
        )
        parsed = result.parsed_json if result.ok and isinstance(result.parsed_json, dict) else None
        if not parsed:
            return []
        profiles = self._coerce_profiles(parsed.get("concept_profiles"), {item.concept_id for item in targets})
        if not profiles:
            return []
        now = utcnow()
        saved: list[dict[str, Any]] = []
        for profile in profiles:
            body = profile.model_dump(mode="json")
            body["updated_at"] = now.isoformat()
            body["updated_by_evidence_id"] = evidence.evidence_id
            self.repo.save_memory(LearningMemory(
                memory_id=profile_memory_id(project_id, profile.concept_id),
                project_id=project_id,
                kind=MemoryKind.LEARNER_PROFILE,
                concept_id=profile.concept_id,
                content=profile.summary,
                metadata={
                    "profile": body,
                    "profile_prompt_version": PROFILE_PROMPT_VERSION,
                    "model": result.model,
                    "evidence_ids": [e.evidence_id for e in self.repo.evidence_for(project_id, profile.concept_id)[-_MAX_FACTS:]],
                    "updated_by_evidence_id": evidence.evidence_id,
                },
                created_at=now,
                updated_at=now,
            ))
            saved.append(body)
        return saved

    def _target_concepts(self, project_id: str, ids: list[str]) -> list[Any]:
        available = {
            concept.concept_id: concept
            for book_id in self.repo.allowed_book_ids(project_id)
            for concept in self.repo.concepts_for_book(book_id)
        }
        return [available[item] for item in ids if item in available]

    def _messages(
        self,
        *,
        project_id: str,
        targets: list[Any],
        task: TrustedTaskContext,
        interaction: InteractionContext,
        judgment: AnswerJudgment,
        answer_text: str,
        evidence: Evidence,
    ) -> list[dict[str, str]]:
        target_text = "\n".join(
            f"- id={concept.concept_id}; 名称={concept.name}; 说明={_short(concept.description, 420)}"
            for concept in targets
        )
        old_profiles = {
            concept.concept_id: profile_for(self.repo, project_id, concept.concept_id)
            for concept in targets
        }
        facts = {
            concept.concept_id: [_fact_view(item) for item in self.repo.evidence_for(project_id, concept.concept_id)[-_MAX_FACTS:]]
            for concept in targets
        }
        criteria = [
            {"criterion": item.criterion_id, "satisfied": item.satisfied, "note": _short(item.note, 180)}
            for item in judgment.criterion_results
        ]
        system = (
            "你是学习档案解释器。你的任务是从可追溯学习事实中生成简洁、可行动的学习画像，"
            "而不是重新判分或直接授予掌握等级。不要预设学科能力分类；用本题和教材学习单元的自然语言描述理解、遗漏、混淆或下一步。"
            "只可更新给定 concept_id；无法可靠归因时不要猜测，可返回空数组。"
            "独立作答与看过提示或讲解后的作答必须明确区分；问题、提示、跳过和讲解只能说明学习过程，不能声称已验证掌握。"
            "不得复述学生完整答案、标准答案或教材长文。每项最多 3 条短语，summary 和 next_practice_goal 各不超过 120 个中文字符。"
            "输出严格 JSON："
            '{"concept_profiles":[{"concept_id":"...","summary":"...","observed_understanding":["..."],'
            '"needs_attention":["..."],"next_practice_goal":"...","confidence":0.0,"evidence_basis":["..."]}]}。'
        )
        user = (
            f"可更新的教材学习单元：\n{target_text}\n\n"
            f"本题验证等级：{', '.join(item.value for item in task.evidence_for_levels)}\n"
            f"作答条件：独立={evidence.independent}；提示次数={interaction.hints_issued}；题目类型={evidence.evidence_type.value}\n"
            f"官方判分结论：{judgment.result.value if judgment.result else '不确定'}；判分理由：{_short(judgment.reason, 280)}\n"
            f"逐项判分：{criteria}\n"
            f"本次学生作答（仅供诊断，不得完整复述）：{_short(answer_text, 1200)}\n\n"
            f"既有画像（可能为空、且必须被新事实纠正）：{old_profiles}\n"
            f"不可篡改的近期事实：{facts}"
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    @staticmethod
    def _coerce_profiles(raw: Any, allowed_ids: set[str]) -> list[ConceptProfile]:
        if not isinstance(raw, list):
            return []
        out: list[ConceptProfile] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            concept_id = str(item.get("concept_id") or "")
            if concept_id not in allowed_ids or concept_id in seen:
                continue
            try:
                profile = ConceptProfile(
                    concept_id=concept_id,
                    summary=_short(item.get("summary"), 160),
                    observed_understanding=_short_list(item.get("observed_understanding"), 3, 100, as_list=True),
                    needs_attention=_short_list(item.get("needs_attention"), 3, 100, as_list=True),
                    next_practice_goal=_short(item.get("next_practice_goal"), 160),
                    confidence=max(0.0, min(1.0, float(item.get("confidence", 0.0) or 0.0))),
                    evidence_basis=_short_list(item.get("evidence_basis"), 3, 130, as_list=True),
                )
            except (TypeError, ValueError):
                continue
            if not (profile.summary or profile.observed_understanding or profile.needs_attention or profile.next_practice_goal):
                continue
            seen.add(concept_id)
            out.append(profile)
        return out


def _fact_view(evidence: Evidence) -> dict[str, Any]:
    return {
        "evidence_id": evidence.evidence_id,
        "type": evidence.evidence_type.value,
        "result": evidence.result.value if evidence.result else None,
        "independent": evidence.independent,
        "hint_level": int(evidence.hint_level),
        "level": evidence.required_level.value,
        "when": evidence.occurred_at.isoformat(),
        "summary": _short(evidence.content_summary, 220),
    }


def _short(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].rstrip()


def _short_list(value: Any, count: int, limit: int, *, as_list: bool = False):
    items = [_short(item, limit) for item in value] if isinstance(value, list) else []
    items = [item for item in items if item][:count]
    return items if as_list else "；".join(items)
