"""Resolve a learner question before textbook retrieval.

Retrieval answers where the evidence is; it must never decide what the
learner meant. Persisted graph nodes are conservative, heading-sized learning
units. Terms inside a heading are aliases for retrieval, not mastery records.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from ..llm.router import ModelRouter
from ..storage.protocols import Repository
from .concept_scope import is_learning_concept, is_learning_section, normalise_section_name


QueryKind = Literal["DIRECT", "COMPARE", "RECOMMEND", "CROSS_DOMAIN", "SELECTION"]
FollowupRelation = Literal["FOLLOW_UP", "NEW_TOPIC", "AMBIGUOUS"]


@dataclass(frozen=True)
class ResolvedConcept:
    """A graph learning unit identified independently of retrieval rank."""

    concept_id: str
    name: str
    book_id: str
    chunk_ids: tuple[str, ...]
    pages: tuple[int, ...]
    confidence: float
    rationale: str


@dataclass(frozen=True)
class QuestionAnalysis:
    """Stable semantic plan used by QA and QUESTION evidence writing."""

    subjects: tuple[ResolvedConcept, ...]
    kind: QueryKind
    needs_general_supplement: bool = False
    selection_chunk_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FollowupResolution:
    relation: FollowupRelation
    subjects: tuple[ResolvedConcept, ...] = ()
    confidence: float = 0.0
    candidate_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Candidate:
    concept: object
    aliases: tuple[str, ...]
    score: int
    matched_aliases: tuple[str, ...]


class ConceptResolver:
    """Project-scoped query understanding over stable learning units.

    A question may name no subject or any number of subjects. Model/context
    limits are safeguards only, never a product rule such as “two concepts”.
    """

    def __init__(self, repo: Repository, router: ModelRouter) -> None:
        self.repo = repo
        self.router = router

    def analyse(
        self,
        *,
        project_id: str,
        question: str,
        source_id: str = "",
        physical_page: int | None = None,
        selection_text: str = "",
    ) -> QuestionAnalysis:
        folded = _normalise(question)
        kind = _query_kind(folded, bool(selection_text))
        direct = [item for item in self._all_candidates(project_id, folded) if item is not None]
        # Lexical overlap is a useful recall hint, not a semantic verdict.
        # In particular, a learner can ask about an operation ("入队") without
        # naming its unit ("队列"), or use a new expression not anticipated by
        # the book mapper.  When a model is available, let it select among the
        # project graph's units; retain exact matching only as an offline/error
        # fallback.  The model still cannot invent a learning-state target.
        semantic_pool = self._semantic_candidates(project_id, folded, direct)
        subjects = self._llm_arbitrate(question, semantic_pool) if getattr(self.router.cfg, "live", False) else []
        if not subjects:
            subjects = self._select_direct(direct)

        # Page-local selection may become a subject only after its characters
        # are verified against server-side chunks. Browser text alone never
        # writes a learning record.
        selection_chunk_ids: tuple[str, ...] = ()
        if selection_text and source_id and physical_page:
            selected, contextual = self._resolve_verified_selection(
                project_id=project_id, source_id=source_id,
                physical_page=physical_page, selection_text=selection_text,
            )
            selection_chunk_ids = tuple(selected)
            if contextual and not subjects:
                subjects = [contextual]

        return QuestionAnalysis(
            subjects=tuple(subjects), kind=kind,
            needs_general_supplement=_needs_general_supplement(folded),
            selection_chunk_ids=selection_chunk_ids,
        )

    def resolve(self, *, project_id: str, question: str) -> list[ResolvedConcept]:
        """Compatibility wrapper for callers that only need subjects."""
        return list(self.analyse(project_id=project_id, question=question).subjects)

    def resolve_contextual_followup(
        self, *, project_id: str, question: str, conversation_context: str,
    ) -> FollowupResolution:
        """Resolve an ellipsis/pronoun against known units, never retrieval rank.

        This is deliberately called only after direct resolution failed and a
        conversational deictic was detected by the caller.  The model may
        select only graph units supplied here, or select none when the
        reference is ambiguous; it cannot invent a topic from a retrieved
        chunk.
        """
        if not conversation_context.strip() or not getattr(self.router.cfg, "live", False):
            return FollowupResolution(relation="AMBIGUOUS")
        # _all_candidates("" ) contains no lexical matches; enumerate the
        # eligible stable units directly for this small, project-local choice.
        candidates = [
            _Candidate(concept, _aliases_for(concept), 0, ())
            for book_id in sorted(self.repo.allowed_book_ids(project_id))
            for concept in self.repo.concepts_for_book(book_id)
            if _eligible_learning_unit(concept)
        ][:96]
        if not candidates:
            return FollowupResolution(relation="AMBIGUOUS")
        cards = "\n".join(
            f"- id={item.concept.concept_id}; 单元={item.concept.name}; 位置={item.concept.section or item.concept.chapter}"
            for item in candidates
        )
        try:
            result = self.router.complete(
                "followup_concept_resolution",
                [{"role": "system", "content": (
                    "你判断当前学习者的话是否指向对话中刚刚讨论的教材学习单元。"
                    "只能从候选单元选择，或选择空数组；不要凭相邻章节猜测。"
                    "relation 只能是 FOLLOW_UP、NEW_TOPIC、AMBIGUOUS。"
                    "只有确实指代前文时才返回 FOLLOW_UP 和 concept_ids。"
                    "AMBIGUOUS 时可返回最可能的 candidate_ids 供用户澄清。"
                    "输出严格 JSON：{\"relation\":\"...\",\"concept_ids\":[\"...\"],\"candidate_ids\":[\"...\"],\"confidence\":0到1}。"
                )}, {"role": "user", "content": (
                    f"最近对话：\n{conversation_context[:1800]}\n\n当前问题：{question[:700]}\n\n候选单元：\n{cards}"
                )}],
                output_schema={"type": "object"}, temperature=0.0, max_tokens=320,
            )
            parsed = result.parsed_json if result.ok else None
            confidence = float((parsed or {}).get("confidence") or 0)
            relation = str((parsed or {}).get("relation") or "")
            raw_ids = (parsed or {}).get("concept_ids") or []
            raw_candidate_ids = (parsed or {}).get("candidate_ids") or []
        except (TypeError, ValueError, AttributeError):
            return FollowupResolution(relation="AMBIGUOUS")
        allowed = {item.concept.concept_id: item for item in candidates}
        candidate_names = tuple(
            allowed[str(value)].concept.name for value in raw_candidate_ids
            if str(value) in allowed
        )[:3]
        if relation not in {"FOLLOW_UP", "NEW_TOPIC", "AMBIGUOUS"}:
            relation = "AMBIGUOUS"
        if relation != "FOLLOW_UP" or confidence < 0.80:
            return FollowupResolution(
                relation=relation if relation != "FOLLOW_UP" else "AMBIGUOUS",
                confidence=confidence, candidate_names=candidate_names,
            )
        selected = [allowed[str(value)] for value in raw_ids if str(value) in allowed]
        # _select_direct already returns ResolvedConcept. Only adjust the
        # provenance/confidence; never feed that result back into _resolved.
        resolved = tuple(
            ResolvedConcept(**{**item.__dict__, "confidence": confidence, "rationale": "llm_followup_resolution"})
            for item in self._select_direct(selected)
        )
        return FollowupResolution(
            relation="FOLLOW_UP" if resolved else "AMBIGUOUS",
            subjects=resolved, confidence=confidence, candidate_names=candidate_names,
        )

    def evidence_chunk_ids(
        self, *, project_id: str, subjects: tuple[ResolvedConcept, ...] | list[ResolvedConcept],
    ) -> list[str]:
        """Return a deterministic section envelope for each resolved unit."""
        by_id = {chunk.chunk_id: chunk for chunk in self.repo.chunks_for_project(project_id)}
        ordered: list[str] = []
        seen: set[str] = set()
        for subject in subjects:
            # A learning unit is one smallest reliable section. Old graphs
            # can contain a broad noun whose references span a whole book;
            # never make all of those historic anchors this question's scope.
            paths: set[tuple[str, ...]] = set()
            for concept in self.repo.concepts_for_book(subject.book_id):
                if concept.concept_id == subject.concept_id:
                    canonical = _canonical_section_path(concept)
                    if canonical:
                        paths.add(canonical)
            # Resolution happens before BookQA lazily restores the persisted
            # chunks after a backend restart. Preserve the graph's exact
            # chunk anchors while that in-memory index is empty; BookQA still
            # validates them against its server-scoped corpus before use.
            ids = list(subject.chunk_ids) if not by_id else [
                chunk_id for chunk_id in subject.chunk_ids
                if chunk_id in by_id
                and (not paths or tuple(by_id[chunk_id].section_path) in paths)
            ]
            ids.extend(
                chunk.chunk_id for chunk in by_id.values()
                if chunk.book_id == subject.book_id and tuple(chunk.section_path) in paths
            )
            for chunk_id in ids:
                if (not by_id or chunk_id in by_id) and chunk_id not in seen:
                    seen.add(chunk_id)
                    ordered.append(chunk_id)
        return ordered

    def _all_candidates(self, project_id: str, question_folded: str) -> list[_Candidate | None]:
        return [
            self._candidate(concept, question_folded)
            for book_id in sorted(self.repo.allowed_book_ids(project_id))
            for concept in self.repo.concepts_for_book(book_id)
            if _eligible_learning_unit(concept)
        ]

    def _candidate(self, concept, question_folded: str) -> _Candidate | None:
        aliases = _aliases_for(concept)
        matched = [alias for alias in aliases if len(alias) >= 2 and alias in question_folded]
        # “二叉树及其表示” is the reliable home for “二叉树是什么”. The
        # shorter base is a resolver alias only, never a graph node. We do
        # not split coordinating headings such as “栈与递归”.
        if not matched:
            matched = [
                base for alias in aliases
                if (base := _heading_base_alias(alias)) and base in question_folded
            ]
        # 栈、树、图 are valid one-character technical subjects, but too noisy
        # in prose unless this looks like an actual concept question.
        if not matched and _looks_like_concept_question(question_folded):
            matched = [alias for alias in aliases if len(alias) == 1 and alias in question_folded]
        if not matched:
            return None
        score = max(len(alias) * 100 + (25 if len(alias) > 1 else 0) for alias in matched)
        return _Candidate(concept=concept, aliases=aliases, score=score, matched_aliases=tuple(matched))

    def _select_direct(self, candidates: list[_Candidate]) -> list[ResolvedConcept]:
        """Keep every explicit unit while collapsing aliases of one section."""
        candidates.sort(key=lambda item: (
            -item.score, not _is_heading_unit(item.concept), not is_learning_concept(item.concept),
            -getattr(item.concept, "importance", 0.0), item.concept.name,
        ))
        selected: list[_Candidate] = []
        seen_ids: set[str] = set()
        seen_units: set[tuple[str, str]] = set()
        seen_matched_topics: set[str] = set()
        for item in candidates:
            cid = item.concept.concept_id
            # During migration an old graph can contain a heading plus an
            # extracted inner noun. Retain one record for that same section.
            unit_key = (item.concept.book_id, _canonical_section(item.concept))
            # Multiple old headings can expose the same short base alias (for
            # example §5.1 and §5.3 both contain “二叉树”). One question must
            # not write evidence to each of them. Distinct subjects in a
            # comparison keep distinct aliases and remain independent.
            matched_topic = _topic_label(item.matched_aliases[0]) if item.matched_aliases else ""
            if cid in seen_ids or unit_key in seen_units or (matched_topic and matched_topic in seen_matched_topics):
                continue
            seen_ids.add(cid)
            seen_units.add(unit_key)
            if matched_topic:
                seen_matched_topics.add(matched_topic)
            selected.append(item)
        return [self._resolved(item, confidence=1.0, rationale="explicit_alias") for item in selected]

    def _semantic_candidates(
        self, project_id: str, question_folded: str, direct: list[_Candidate] | None = None,
    ) -> list[_Candidate]:
        """Give semantic resolution a broad graph view, lexically ranked only for cost.

        The rank here never selects a concept.  It merely keeps the LLM prompt
        tractable for unusually large books while preserving direct matches and
        a representative project-local tail for terminology the mapper did not
        anticipate.
        """
        query_terms = _terms(question_folded)
        ranked: list[_Candidate] = []
        for book_id in sorted(self.repo.allowed_book_ids(project_id)):
            for concept in self.repo.concepts_for_book(book_id):
                if not _eligible_learning_unit(concept):
                    continue
                aliases = _aliases_for(concept)
                card_terms = set().union(*(_terms(alias) for alias in aliases)) if aliases else set()
                overlap = query_terms & card_terms
                ranked.append(_Candidate(
                    concept, aliases, sum(map(len, overlap)), tuple(sorted(overlap)),
                ))
        ranked.sort(key=lambda item: (-item.score, -getattr(item.concept, "importance", 0.0), item.concept.name))
        by_id = {item.concept.concept_id: item for item in ranked}
        ordered = list(direct or []) + ranked
        chosen: list[_Candidate] = []
        seen: set[str] = set()
        for item in ordered:
            if item.concept.concept_id in seen:
                continue
            seen.add(item.concept.concept_id)
            chosen.append(by_id.get(item.concept.concept_id, item))
            if len(chosen) >= 96:  # prompt-size guard, never a subject-count rule
                break
        return chosen

    def _llm_arbitrate(self, question: str, candidates: list[_Candidate]) -> list[ResolvedConcept]:
        if not candidates or not getattr(self.router.cfg, "live", False):
            return []
        cards = "\n".join(
            f"- id={item.concept.concept_id}; 学习单元={item.concept.name}; "
            f"章节={item.concept.section or item.concept.chapter}; 别名={','.join(item.aliases[:8])}"
            for item in candidates
        )
        try:
            result = self.router.complete(
                "question_concept_resolution",
                [{"role": "system", "content": (
                    "你是教材问题的知识点解析器。只能从候选学习单元中选择与问题直接相关的0个、1个或多个单元；"
                    "结合问题语义判断，而不要把关键词重合或章节相邻当作充分理由。操作、应用、比较、代码和自然语言别称"
                    "都可能指向一个学习单元；没有可靠对应时 concept_ids 必须为空。"
                    "当学习者询问‘我还缺什么/我学到哪里/下一步’这类个人学习反思时，选择的是用户明确提到、"
                    "声称已阅读或正在询问的学习单元；绝不能因为某个更窄的小节适合作为未来练习，就把它当作当前主题。"
                    "若用户只说一个上位概念而没有点名具体操作，优先选择名称或别名直接包含该概念的概览/定义单元；"
                    "只有用户明确提及某项操作、性质或算法时，才选择对应的窄小节。"
                    "输出严格 JSON：{\"concept_ids\":[\"...\"],\"confidence\":0到1}。"
                )}, {"role": "user", "content": f"问题：{question[:700]}\n候选：\n{cards}"}],
                output_schema={"type": "object"}, temperature=0.0, max_tokens=240,
            )
            parsed = result.parsed_json if result.ok else None
            raw_ids = (parsed or {}).get("concept_ids") or []
            confidence = float((parsed or {}).get("confidence") or 0)
        except (TypeError, ValueError, AttributeError):
            return []
        if confidence < 0.82:
            return []
        allowed = {item.concept.concept_id: item for item in candidates}
        selected = [allowed[str(value)] for value in raw_ids if str(value) in allowed]
        unique = self._select_direct(selected)
        return [ResolvedConcept(**{**item.__dict__, "confidence": confidence, "rationale": "llm_candidate_arbitration"}) for item in unique]

    def _resolve_verified_selection(
        self, *, project_id: str, source_id: str, physical_page: int, selection_text: str,
    ) -> tuple[list[str], ResolvedConcept | None]:
        needle = _normalise(selection_text)
        if len(needle) < 8:
            return [], None
        matches = []
        for chunk in self.repo.chunks_for_project(project_id):
            if chunk.book_id != source_id:
                continue
            start = chunk.page_start or chunk.source_ref.physical_page
            end = chunk.page_end or start
            if start <= physical_page <= end and needle in _normalise(chunk.content):
                matches.append(chunk)
        if not matches:
            return [], None
        selected_ids = [chunk.chunk_id for chunk in matches]
        paths = {tuple(chunk.section_path) for chunk in matches if chunk.section_path}
        possibilities: list[_Candidate] = []
        for concept in self.repo.concepts_for_book(source_id):
            if not is_learning_concept(concept):
                continue
            ref_paths = {tuple(ref.section_path) for ref in concept.source_refs if ref.section_path}
            if paths & ref_paths:
                possibilities.append(_Candidate(concept, _aliases_for(concept), 1, ()))
        if len(possibilities) != 1:
            return selected_ids, None
        return selected_ids, self._resolved(possibilities[0], confidence=1.0, rationale="verified_selection")

    @staticmethod
    def _resolved(candidate: _Candidate, *, confidence: float, rationale: str) -> ResolvedConcept:
        refs = list(getattr(candidate.concept, "source_refs", ()) or ())
        canonical_path = _canonical_section_path(candidate.concept)
        if canonical_path:
            refs = [ref for ref in refs if tuple(getattr(ref, "section_path", ()) or ()) == canonical_path]
        return ResolvedConcept(
            concept_id=candidate.concept.concept_id,
            name=candidate.concept.name,
            book_id=candidate.concept.book_id,
            chunk_ids=tuple(dict.fromkeys(ref.chunk_id for ref in refs if ref.chunk_id)),
            pages=tuple(dict.fromkeys(ref.physical_page for ref in refs if ref.physical_page)),
            confidence=confidence,
            rationale=rationale,
        )


def _normalise(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").casefold())


def _aliases_for(concept) -> tuple[str, ...]:
    # ``concept.section`` is a full breadcrumb (for example
    # “第4章 栈与队列 · §4.2 栈与递归”). Treating that whole string as an
    # alias leaks the chapter word “栈” to every sibling section. Only the
    # leaf is a subject alias; the full path remains provenance, not meaning.
    section = getattr(concept, "section", "") or ""
    labels = [getattr(concept, "name", ""), section.split("·")[-1].strip()]
    # Do not harvest every SourceRef leaf as an alias. Legacy graph merges can
    # legitimately accumulate references across an entire chapter; doing so
    # would make an unrelated node (for example “序”) claim aliases such as
    # “栈”. ``name`` and the canonical ``section`` breadcrumb are stable
    # identity fields and are sufficient for the conservative policy.
    aliases: set[str] = set()
    for label in labels:
        topic = _topic_label(label)
        if topic:
            aliases.add(topic)
            # A compound heading is itself a topic. Splitting “栈与递归”
            # into the one-character alias “栈” makes every sibling-looking
            # section claim a basic Stack question. Keep the complete phrase;
            # the actual §4.1 栈 unit supplies the precise short alias.
    return tuple(sorted(aliases, key=lambda value: (-len(value), value)))


def _topic_label(value: str) -> str:
    compact = normalise_section_name(value)
    compact = re.sub(r"^第[一二三四五六七八九十百\d]+章", "", compact)
    compact = re.sub(r"^[§*]?\d+(?:\.\d+)*[、.．]?", "", compact)
    return compact.strip()


def _heading_base_alias(value: str) -> str:
    """Extract an unambiguous named subject from a descriptive heading."""
    topic = _topic_label(value)
    # “二叉树及其表示” / “二叉树的实现” have a stable named subject.
    # “栈与递归” deliberately remains an indivisible topic.
    for marker in ("及其", "的"):
        if marker in topic:
            base = topic.split(marker, 1)[0].strip()
            return base if len(base) >= 2 else ""
    return ""


def _canonical_section(concept) -> str:
    path = _canonical_section_path(concept)
    if path:
        return " · ".join(path)
    return getattr(concept, "section", "") or getattr(concept, "name", "")


def _canonical_section_path(concept) -> tuple[str, ...]:
    """Return one section identity, never a union of legacy references."""
    for ref in getattr(concept, "source_refs", ()) or ():
        path = tuple(getattr(ref, "section_path", ()) or ())
        if len(path) >= 2 and is_learning_section(path[-1], path, require_leaf=True):
            return path
    for ref in getattr(concept, "source_refs", ()) or ():
        path = tuple(getattr(ref, "section_path", ()) or ())
        if path:
            return path
    return ()


def _eligible_learning_unit(concept) -> bool:
    """Allow old coarse units during migration, never publishing matter."""
    # A real learning unit may contain several chunks, but they share one
    # section path. Cross-section SourceRefs reveal an old aggregate/topic
    # node and are unsafe for both mastery evidence and constrained retrieval.
    paths = {
        tuple(getattr(ref, "section_path", ()) or ())
        for ref in getattr(concept, "source_refs", ()) or ()
        if getattr(ref, "section_path", ())
    }
    if len(paths) > 1:
        return False
    if is_learning_concept(concept):
        return True
    # Earlier mappings sometimes labelled a section unit with its chapter
    # name, which ``is_learning_concept`` deliberately rejects as an
    # aggregate. If it is nevertheless anchored to a concrete teachable leaf,
    # keep it available until the book is remapped to the heading-unit model.
    for ref in getattr(concept, "source_refs", ()) or ():
        path = tuple(getattr(ref, "section_path", ()) or ())
        if len(path) >= 2 and is_learning_section(path[-1], path, require_leaf=True):
            return True
    return False


def _is_heading_unit(concept) -> bool:
    """Prefer the canonical smallest heading over legacy in-prose nodes."""
    name = normalise_section_name(getattr(concept, "name", ""))
    section = (getattr(concept, "section", "") or "").split("·")[-1].strip()
    if name and name == normalise_section_name(section):
        return True
    for ref in getattr(concept, "source_refs", ()) or ():
        path = tuple(getattr(ref, "section_path", ()) or ())
        if path and name == normalise_section_name(path[-1]):
            return True
    return False


def _terms(value: str) -> set[str]:
    terms = set(re.findall(r"[a-z][a-z0-9_+#.-]{1,}", value))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        terms.add(run)
        # Chinese has no word boundary. Bounded n-grams let “二叉树是什么”
        # expose the subject “二叉树” without blindly accepting a one-letter
        # match or treating every full sentence as one topic.
        for width in range(2, min(6, len(run)) + 1):
            terms.update(run[index:index + width] for index in range(len(run) - width + 1))
    return terms


def _looks_like_concept_question(value: str) -> bool:
    return any(marker in value for marker in ("什么", "作用", "区别", "比较", "怎么", "如何", "复杂度", "实现", "代码", "为什么"))


def _query_kind(value: str, has_selection: bool) -> QueryKind:
    if has_selection:
        return "SELECTION"
    if any(marker in value for marker in ("区别", "不同", "比较", "对比", "优缺点", "优势")):
        return "COMPARE"
    if any(marker in value for marker in ("用哪个", "应该用", "选哪个", "推荐", "适合", "怎么选")):
        return "RECOMMEND"
    if any(marker in value for marker in ("汇编", "操作系统", "浏览器", "数据库", "网络", "c语言", "c++", "python", "工程", "项目")):
        return "CROSS_DOMAIN"
    return "DIRECT"


def _needs_general_supplement(value: str) -> bool:
    # This is only the offline/error fallback.  It must recognise the broad
    # intent "show me code", rather than require one exact phrasing such as
    # "代码怎么写".  The answer route itself still clearly labels this as
    # general knowledge and never turns it into textbook evidence.
    return any(marker in value for marker in (
        "汇编", "操作系统", "浏览器", "数据库", "网络", "c语言", "c++", "python",
        "代码", "实现", "示例", "应用", "怎么用", "怎么描述",
    ))


__all__ = ["ConceptResolver", "QuestionAnalysis", "ResolvedConcept"]
