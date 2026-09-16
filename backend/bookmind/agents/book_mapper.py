"""Book Mapper Agent — ARCHITECTURE.md §3.1, LEARNING_MODEL.md §2.

Turns one section's text into :class:`ConceptProposal` / :class:`RelationProposal`
drafts. The model is asked for structured JSON; on any failure the agent
degrades to a deterministic offline extractor (keyword/title heuristics) so
the graph can still be built in CI and the offline demo.

Constraints (§3.1):
  - prerequisites are *proposals only* (the Engine validates them);
  - the gold skeleton is never overwritten (the Engine enforces this);
  - never accesses learner state;
  - every proposal carries a source_ref back to the section's blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import Difficulty, RelationType
from ..domain.proposals import ConceptProposal, RelationProposal, SectionProposal
from ..domain.source_ref import SourceRef
from ..llm.router import ModelRouter
from ..llm.schemas import ModelResult
from ..retrieval.chunk import DocumentChunk
from ..retrieval.parsed_document import Section


BOOK_MAPPER_PROMPT_VERSION = "book_mapper_v1"

# A small, deterministic keyword→difficulty map used by the offline extractor
# AND to coerce model outputs. Keeps difficulty consistent across paths.
_DIFFICULTY_KEYWORDS = {
    Difficulty.HARD: ["契约", "contract", "底层", "内部", "原理", "泛型", "generic",
                      "多态", "polymorphism", "动态分派", "dispatch", "hashcode",
                      "并发", "concurrent", "内存模型"],
    Difficulty.EASY: ["变量", "variable", "注释", "注释", "基本类型", "语法", "入门",
                      "声明", "赋值"],
}


@dataclass
class BookMapperAgent:
    """Extracts concept proposals from one section at a time."""

    router: ModelRouter

    def map_section(
        self,
        section: Section,
        chunks: list[DocumentChunk],
        *,
        known_concept_names: list[str] | None = None,
        extract_definitions: bool = True,
    ) -> SectionProposal:
        """Propose concepts + relations for one section.

        ``known_concept_names`` (the gold skeleton + already-built concepts) is
        passed to the model so it can reuse existing names in prerequisite
        proposals rather than inventing near-duplicates. The offline extractor
        also uses it to link prerequisites.
        """
        if not chunks:
            return SectionProposal(section_id=section.section_id,
                                   section_path=section.section_path, fallback=True)
        # The ingestion runner deliberately uses an offline mapper so a large
        # book cannot create hundreds of serial API calls. Go directly to the
        # deterministic extractor instead of calling the router once per
        # section and flooding logs with expected fallback warnings.
        # A test/private deployment may inject a router subclass whose
        # ``complete`` implementation is available without the built-in HTTP
        # provider. Honour that dependency-injection seam; only force the
        # deterministic path for the stock router when it is truly offline.
        has_injected_completion = type(self.router).complete is not ModelRouter.complete
        if not self.router.live_available and not has_injected_completion:
            return self._offline_extract(
                section, chunks, known_concept_names or [],
                extract_definitions=extract_definitions,
            )
        res = self._call_model(section, chunks, known_concept_names or [])
        if res.ok and res.parsed_json is not None:
            proposal = _coerce_section(res.parsed_json, section, chunks)
            proposal.fallback = res.fallback
            return proposal
        # Offline / model-failure path: deterministic keyword extraction.
        return self._offline_extract(
            section, chunks, known_concept_names or [],
            extract_definitions=extract_definitions,
        )

    # --- live model path --------------------------------------------------

    def _call_model(self, section: Section, chunks: list[DocumentChunk],
                    known: list[str]) -> ModelResult:
        context = "\n\n".join(
            f"[CHUNK {i+1}] id={c.chunk_id} page={c.source_ref.physical_page}\n{c.content}"
            for i, c in enumerate(chunks)
        )
        known_txt = ", ".join(known) if known else "（暂无）"
        system = (
            "你是学习资料概念图谱抽取器。从给定的小节文本中抽取核心概念与先修关系，输出严格 JSON。"
            "要求：1) 只抽取本小节真正讲解的概念；2) 先修关系只提议，由后续校验决定；"
            "3) 尽量复用已知概念名，避免近义重复；4) 每个概念给出资料来源 chunk_id 与页码。"
            'JSON 格式: {"concepts":[{"name":"...","description":"...","importance":0.0-1.0,'
            '"difficulty":"EASY|MEDIUM|HARD","prerequisites":["已知概念名"],"rationale":"..."}],'
            '"relations":[{"source":"概念名","target":"先修概念名","relation":"PREREQUISITE|RELATED"}]}。'
            "不要输出 JSON 以外的内容。"
        )
        user = f"已知概念: {known_txt}\n小节: {' · '.join(section.section_path)}\n资料片段:\n{context}"
        return self.router.complete(
            "book_mapper_map_section",
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            output_schema={"type": "object"},
            temperature=0.0,
        )

    # --- offline deterministic path ---------------------------------------

    def _offline_extract(
        self, section: Section, chunks: list[DocumentChunk], known: list[str],
        *, extract_definitions: bool = True,
    ) -> SectionProposal:
        """Deterministic concept extraction for the no-model path.

        Strategy (three sources of candidates, all chunk-anchored):
          1. **Known-name match**: a chunk that mentions a known concept name
             anchors a proposal for that concept, plus RELATED edges between
             co-occurring known names in the same chunk.
          2. **Title-derived**: the section title itself becomes one candidate
             concept (so a genuinely new section still produces a node), unless
             it already matches a known name.
          3. **Sentence-led new concepts**: for chunks that define a new term
             (a sentence containing a definitional cue like "是"/"指"/"称为"/
             "is"/"means"), the leading noun phrase of that sentence becomes a
             new candidate concept. This is what lets the offline path extend
             the graph toward 50–80 concepts, not just match the gold 30.

        Proposed prerequisites: each new candidate links to the known concepts
        its chunk mentions (a real, grounded prerequisite proposal — the Engine
        validates it for cycles). This is deliberately conservative and fully
        deterministic.
        """
        known_lower = {n.lower(): n for n in known}
        concepts: list[ConceptProposal] = []
        relations: list[RelationProposal] = []
        seen_names: set[str] = set()
        chapter = section.section_path[0] if section.section_path else ""
        section_lbl = " · ".join(section.section_path)

        def _ref(c: DocumentChunk) -> SourceRef:
            return SourceRef(
                document_id=c.source_ref.document_id, chunk_id=c.chunk_id,
                block_id=c.source_ref.block_id, physical_page=c.source_ref.physical_page,
                section_path=c.section_path,
            )

        # (2) Title-derived candidate (first, so it gets the section's main ref).
        # Strip a leading chapter number ("10 泛型" → "泛型") so the title
        # concept merges with a sentence-derived concept of the same short name.
        title = section.title.strip()
        title = _strip_leading_number(title)
        if title and title.lower() not in seen_names:
            seen_names.add(title.lower())
            first_ref = _ref(chunks[0]) if chunks else None
            concepts.append(ConceptProposal(
                name=title, chapter=chapter, section=section_lbl,
                importance=0.45, difficulty=_guess_difficulty(chunks[0].content if chunks else ""),
                source_refs=[first_ref] if first_ref else [],
                rationale="offline: derived from section title",
            ))

        for c in chunks:
            text = c.content
            text_lower = text.lower()
            hits = [orig for low, orig in known_lower.items() if low in text_lower]
            ref = _ref(c)

            # (1) Known-name matches anchor proposals + RELATED co-occurrence.
            for name in hits:
                if name.lower() in seen_names:
                    continue
                seen_names.add(name.lower())
                concepts.append(ConceptProposal(
                    name=name, chapter=chapter, section=section_lbl,
                    importance=0.6, difficulty=_guess_difficulty(text),
                    source_refs=[ref], rationale="offline: matched known concept name",
                    proposed_prerequisites=[h for h in hits if h.lower() != name.lower()],
                ))
            for i in range(len(hits)):
                for j in range(i + 1, len(hits)):
                    relations.append(RelationProposal(
                        source_name=hits[i], target_name=hits[j],
                        relation=RelationType.RELATED, rationale="co-occur in chunk",
                        source_refs=[ref],
                    ))

            # (3) Sentence-led new concepts from definitional cues.
            for phrase in _definitional_phrases(text) if extract_definitions else []:
                pname = phrase.strip()
                if not pname or pname.lower() in seen_names:
                    continue
                # Skip if it's just a known concept name restated.
                if pname.lower() in known_lower:
                    continue
                seen_names.add(pname.lower())
                concepts.append(ConceptProposal(
                    name=pname, chapter=chapter, section=section_lbl,
                    importance=0.5, difficulty=_guess_difficulty(text),
                    source_refs=[ref], rationale="offline: definitional sentence",
                    proposed_prerequisites=list(hits),  # link to known concepts mentioned
                ))
        return SectionProposal(
            section_id=section.section_id, section_path=section.section_path,
            concepts=concepts, relations=relations, fallback=True,
        )


def _guess_difficulty(text: str) -> Difficulty:
    t = text.lower()
    for diff, kws in _DIFFICULTY_KEYWORDS.items():
        if any(k.lower() in t for k in kws):
            return diff
    return Difficulty.MEDIUM


import re as _re

_NUM_PREFIX = _re.compile(
    r"^(?:(?:第[一二三四五六七八九十百\d]+章|chapter\s+\d+)\s*|\d+[\s.、]*)",
    _re.IGNORECASE,
)


def _strip_leading_number(title: str) -> str:
    """Remove a leading chapter number from a section title.

    "10 泛型" → "泛型", "3.2 Polymorphism" → "Polymorphism". Leaves titles
    without a numeric prefix unchanged.
    """
    return _NUM_PREFIX.sub("", title).strip()


# Definitional cues that signal "this sentence introduces a term". A sentence
# containing one of these is likely naming a concept; we take the leading
# noun-ish span as the candidate name. Order matters: longer cues first.
_DEFINITION_CUES = [
    "是指", "指的是", "是指的", "称为", "叫做", "称作", "定义为", "是指：",
    " means ", " is defined as ", " is called ", " refers to ",
    "是", "is a", "is an", "are",
]


def _definitional_phrases(text: str) -> list[str]:
    """Extract candidate concept names from definitional sentences in ``text``.

    Splits the text into sentences (by Chinese/English sentence enders), keeps
    those containing a definitional cue, and returns the leading span of each
    (trimmed, ≤12 chars) as a candidate name. Deterministic and conservative —
    a sentence becomes a candidate only if the span before the cue is a short
    noun-ish phrase (not a whole clause), so we get clean concept names like
    "泛型" / "异常" / "接口" rather than sentence fragments.
    """
    import re
    sentences = re.split(r"[。！？；\n.!?;]", text)
    out: list[str] = []
    for s in sentences:
        s = s.strip()
        if len(s) < 4 or len(s) > 60:
            continue
        low = s.lower()
        cue_pos = -1
        for cue in _DEFINITION_CUES:
            idx = low.find(cue)
            if idx == -1:
                continue
            if cue_pos == -1 or idx < cue_pos:
                cue_pos = idx
        if cue_pos <= 0:
            continue
        name = s[:cue_pos].strip(" :：，,、的")
        # Keep only short noun-ish phrases — a real concept name, not a clause.
        if not (2 <= len(name) <= 12):
            continue
        # Reject if it contains a verb-ish cue (likely a clause, not a term).
        if any(v in name for v in ("比较", "存储", "保存", "使用", "包含", "获得")):
            continue
        if name not in out:
            out.append(name)
    return out


def _coerce_section(data: dict, section: Section, chunks: list[DocumentChunk]) -> SectionProposal:
    """Validate a model JSON response into a SectionProposal.

    Defensive: malformed fields are dropped, not raised over, so a bad model
    output never crashes the graph build. Each concept proposal is anchored to
    the chunk it came from using chunk_id hints in the model output when
    present, else to the first chunk.
    """
    chunks_by_id = {c.chunk_id: c for c in chunks}
    concepts: list[ConceptProposal] = []
    for raw in data.get("concepts", []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()
        if not name:
            continue
        # Anchor to the chunk the model cited, else the first chunk.
        ref = _ref_for(raw, chunks_by_id, chunks)
        difficulty = _parse_difficulty(raw.get("difficulty"))
        importance = _parse_importance(raw.get("importance"))
        prereqs = [str(p) for p in raw.get("prerequisites", []) if isinstance(p, str) and p.strip()]
        related = [str(p) for p in raw.get("related", []) if isinstance(p, str) and p.strip()]
        concepts.append(ConceptProposal(
            name=name, description=str(raw.get("description", "")).strip(),
            chapter=section.section_path[0] if section.section_path else "",
            section=" · ".join(section.section_path),
            importance=importance, difficulty=difficulty,
            source_refs=[ref] if ref else [],
            rationale=str(raw.get("rationale", "")).strip(),
            proposed_prerequisites=prereqs, proposed_related=related,
        ))
    relations: list[RelationProposal] = []
    for raw in data.get("relations", []):
        if not isinstance(raw, dict):
            continue
        src = str(raw.get("source", "")).strip()
        tgt = str(raw.get("target", "")).strip()
        if not src or not tgt:
            continue
        rel = RelationType.PREREQUISITE if str(raw.get("relation", "")).upper() == "PREREQUISITE" else RelationType.RELATED
        relations.append(RelationProposal(
            source_name=src, target_name=tgt, relation=rel,
            rationale=str(raw.get("rationale", "")).strip(),
        ))
    return SectionProposal(
        section_id=section.section_id, section_path=section.section_path,
        concepts=concepts, relations=relations, fallback=False,
    )


def _ref_for(raw: dict, chunks_by_id: dict[str, DocumentChunk], chunks: list[DocumentChunk]) -> SourceRef | None:
    cid = raw.get("chunk_id")
    c = chunks_by_id.get(str(cid)) if cid else None
    if c is None and chunks:
        c = chunks[0]
    if c is None:
        return None
    return SourceRef(
        document_id=c.source_ref.document_id, chunk_id=c.chunk_id,
        block_id=c.source_ref.block_id, physical_page=c.source_ref.physical_page,
        section_path=c.section_path,
    )


def _parse_difficulty(val) -> Difficulty:
    s = str(val).upper()
    if s in ("EASY", "MEDIUM", "HARD"):
        return Difficulty(s)
    return Difficulty.MEDIUM


def _parse_importance(val) -> float:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, f))


__all__ = ["BookMapperAgent", "BOOK_MAPPER_PROMPT_VERSION"]
