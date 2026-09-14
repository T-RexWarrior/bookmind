"""Tutor Agent — ARCHITECTURE.md §3.2, §4.2.

Responsibilities:
  - answer textbook questions *with citations* drawn only from retrieved chunks;
  - explain, give examples/analogies/counterexamples;
  - render the Engine's selected action as natural language (verify/review/
    learn-prerequisite/remediate content), adapted to the learner's level;
  - regenerate once if the Citation Validator rejects the answer, and if it
    still fails, state plainly that the textbook lacks sufficient basis.

Constraints (§3.2): never decides next action, never judges or writes mastery,
never writes Evidence, and answers only using chunks from the current context.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import Level
from ..llm.router import ModelRouter
from ..llm.schemas import ModelResult
from ..retrieval.chunk import DocumentChunk
from ..retrieval.citation import CitationReport, CitationValidator
from ..retrieval.fusion import RetrievalHit


TUTOR_PROMPT_VERSION = "tutor_v1"


@dataclass
class TutorAnswer:
    text: str
    citations: list[dict] = field(default_factory=list)
    grounded: bool = True  # False iff citation validation failed twice
    chunk_ids: list[str] = field(default_factory=list)
    regenerated: bool = False
    fallback: bool = False  # True iff a model fallback was used
    reason: str = ""


class TutorAgent:
    """Answers questions with grounded citations, regenerating on failure."""

    def __init__(self, router: ModelRouter, validator: CitationValidator) -> None:
        self.router = router
        self.validator = validator

    def answer(
        self,
        question: str,
        hits: list[RetrievalHit],
        *,
        learner_level: Level = Level.L0,
        max_attempts: int = 2,
        max_tokens: int | None = None,
    ) -> TutorAnswer:
        """Answer a question grounded in ``hits``.

        Flow (ARCHITECTURE §4.2):
          1. build a prompt with the retrieved chunks as context;
          2. ask the model to answer with citations;
          3. validate citations; if any fail, regenerate once from the same
             chunks with the failed citations removed;
          4. if still failing, drop unsupported claims and say the textbook
             lacks sufficient basis — never keep a claim after dropping its
             citation (PRODUCT_SPEC §8).
        """
        chunks = [h.chunk for h in hits]
        context_chunk_ids = [c.chunk_id for c in chunks]
        if not chunks:
            return TutorAnswer(
                text="当前资料范围中未找到与该问题相关的段落，无法给出有依据的回答。",
                grounded=False, reason="no retrieved chunks",
            )

        for attempt in range(max_attempts):
            res = self._call_model(question, chunks, learner_level, attempt, max_tokens)
            if not res.ok:
                # Model unavailable: fall back to a deterministic stitched
                # answer from the top chunk (PRODUCT_SPEC §8 conservative
                # fallback; never crash).
                return self._fallback_answer(question, chunks, res)
            structured = getattr(res, "parsed_json", None)
            if isinstance(structured, dict):
                text = str(structured.get("answer") or "").strip()
                raw_citations = structured.get("citations") or []
                citations = [item for item in raw_citations if isinstance(item, dict)]
            else:
                text, citations = _parse_answer(res.content or "")
            report = self.validator.validate(citations, context_chunk_ids)
            if report.ok:
                return TutorAnswer(
                    text=text, citations=citations, grounded=True,
                    chunk_ids=context_chunk_ids, regenerated=attempt > 0,
                    fallback=res.fallback, reason="ok",
                )
            # On the last attempt, don't retry — produce the honest rejection.
            if attempt == max_attempts - 1:
                return self._rejection(report, chunks)
            # Else: regenerate, hinting the model to use only supported chunks.
        # Unreachable, but keep the type checker calm.
        return TutorAnswer(text="", grounded=False, reason="unreachable")

    # --- model call --------------------------------------------------------

    def _call_model(self, question: str, chunks: list[DocumentChunk], level: Level, attempt: int, max_tokens: int | None = None) -> ModelResult:
        context = "\n\n".join(
            f"[CHUNK {i+1}] id={c.chunk_id} page={c.source_ref.physical_page}\n{c.content}"
            for i, c in enumerate(chunks)
        )
        level_hint = f"学习者当前等级约 {level.value}，请调整表达深度。" if level != Level.L0 else ""
        retry_hint = "\n注意：上一次回答的引用未能通过校验，请只引用上面给出的 CHUNK，并确保 quote 与原文逐字一致。" if attempt > 0 else ""
        system = (
            "你是资料学习助手。只能依据给定的 CHUNK 回答，不得编造资料中没有的内容。"
            "直接回答用户问的内容，不要描述你的检索过程。"
            "如果资料描述了多个版本或改进阶段，且它们的限制不同，必须分阶段说明，不能混为同一个版本。"
            "返回 JSON 对象：answer 是简洁自然语言答案；citations 是引用数组，"
            "每条包含 chunk_id、quote 和 page。quote 必须与原文逐字一致。"
            f"{level_hint}{retry_hint}"
        )
        user = f"资料片段：\n{context}\n\n问题：{question}"
        return self.router.complete(
            "tutor_answer",
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            output_schema={
                "type": "object",
                "required": ["answer", "citations"],
                "properties": {
                    "answer": {"type": "string"},
                    "citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["chunk_id", "quote", "page"],
                            "properties": {
                                "chunk_id": {"type": "string"},
                                "quote": {"type": "string"},
                                "page": {"type": "string"},
                            },
                        },
                    },
                },
            },
            temperature=0.2,
            max_tokens=max_tokens,
        )

    def _fallback_answer(self, question: str, chunks: list[DocumentChunk], res: ModelResult) -> TutorAnswer:
        top = chunks[0]
        reason = res.error or "模型网关未返回有效回答。"
        text = f"（模型暂不可用：{reason}）\n当前先展示相关资料原文摘录：\n{top.content}"
        return TutorAnswer(
            text=text,
            citations=[{"chunk_id": top.chunk_id, "quote": top.content[:40], "page": str(top.source_ref.physical_page)}],
            grounded=True, chunk_ids=[c.chunk_id for c in chunks],
            fallback=True, reason=f"model fallback: {reason}",
        )

    def _rejection(self, report: CitationReport, chunks: list[DocumentChunk]) -> TutorAnswer:
        failed = [c.reason for c in report.checks if not c.ok]
        return TutorAnswer(
            text="无法在当前资料片段中找到足够依据来支持回答，因此不给出可能不准确的论断。请尝试调整问题范围或切换资料位置。",
            grounded=False, chunk_ids=[c.chunk_id for c in chunks],
            reason=f"citation validation failed twice: {failed}",
        )


def _parse_answer(content: str) -> tuple[str, list[dict]]:
    """Split a model answer into prose + a trailing citation list.

    The model is asked to put citations as JSON at the end (an array of objects,
    or a single object for one citation). We isolate the last JSON value in the
    text; the rest is the prose answer. If no citation JSON is found we return
    the whole text as prose with no citations (the CitationValidator will then
    mark the answer ungrounded, triggering regeneration or rejection).
    """
    import json as _json
    import re as _re

    text = content.strip()
    # Preferred structured response, including gateways that ignored
    # response_format but still returned the requested JSON object.
    try:
        whole = _json.loads(text)
        if isinstance(whole, dict) and "answer" in whole:
            citations = whole.get("citations") or []
            return str(whole.get("answer") or "").strip(), [
                item for item in citations if isinstance(item, dict)
            ]
    except _json.JSONDecodeError:
        pass

    # Strip a trailing fenced JSON citation block. Previously the closing
    # backticks made json.loads fail and the raw block was shown to the user.
    fenced = _re.search(r"```(?:json)?\s*([\s\S]*?)\s*```\s*$", text, _re.IGNORECASE)
    if fenced:
        try:
            value = _json.loads(fenced.group(1).strip())
            prose = text[:fenced.start()].strip()
            if isinstance(value, dict) and "answer" in value:
                citations = value.get("citations") or []
                return str(value.get("answer") or prose).strip(), [
                    item for item in citations if isinstance(item, dict)
                ]
            if isinstance(value, list):
                return prose, [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return prose, [value]
        except _json.JSONDecodeError:
            pass
    # Try to peel a trailing JSON value (object or array) off the end. We scan
    # from the end for the last '{' or '[' that opens a balanced JSON value.
    for opener in ("}", "]"):
        idx = text.rfind(opener)
        if idx == -1:
            continue
        # Walk back to the matching opener.
        depth = 0
        for i in range(idx, -1, -1):
            ch = text[i]
            if ch == opener:
                depth += 1
            elif ch == "{" and opener == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[i:]
                    try:
                        val = _json.loads(candidate)
                    except _json.JSONDecodeError:
                        break
                    if isinstance(val, list):
                        return text[:i].strip(), [v for v in val if isinstance(v, dict)]
                    if isinstance(val, dict):
                        return text[:i].strip(), [val]
                    break
            elif ch == "[" and opener == "]":
                depth -= 1
                if depth == 0:
                    candidate = text[i:]
                    try:
                        val = _json.loads(candidate)
                    except _json.JSONDecodeError:
                        break
                    if isinstance(val, list):
                        return text[:i].strip(), [v for v in val if isinstance(v, dict)]
                    if isinstance(val, dict):
                        return text[:i].strip(), [val]
                    break
    return text, []
