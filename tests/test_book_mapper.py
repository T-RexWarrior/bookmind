"""Tests for the Book Mapper Agent (Phase 3).

Covers the offline deterministic extractor (no model) and the JSON coercion
path (with a stubbed router returning model JSON).
"""

from __future__ import annotations

import json

from bookmind.agents.book_mapper import BookMapperAgent
from bookmind.domain.enums import Difficulty, RelationType
from bookmind.domain.source_ref import SourceRef
from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.retrieval.chunk import DocumentChunk
from bookmind.retrieval.parsed_document import Section


def _chunk(cid: str, text: str, page: int, section_path: tuple[str, ...]) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=cid, book_id="b1", document_id="d1", section_path=section_path,
        content=text, source_ref=SourceRef(document_id="d1", chunk_id=cid,
                                           block_id=cid, physical_page=page),
        block_ids=[cid],
    )


def _section(sid: str, title: str, path: tuple[str, ...], page: int = 1) -> Section:
    return Section(section_id=sid, title=title, section_path=path, physical_page=page)


# --- offline path --------------------------------------------------------


def test_offline_extracts_known_concepts_anchored_to_chunks():
    router = ModelRouter(RouterConfig(live=False))  # offline → fallback
    agent = BookMapperAgent(router)
    sec = _section("s1", "4 == 与 equals", ("4 == 与 equals",), 55)
    chunks = [
        _chunk("chk1", "== 运算符比较地址，equals 比较内容。", 55, ("4 == 与 equals",)),
    ]
    known = ["equals", "== operator (reference)", "Variables & primitive types"]
    prop = agent.map_section(sec, chunks, known_concept_names=known)
    assert prop.fallback is True
    names = {c.name for c in prop.concepts}
    # "equals" matched as a substring; "variables" did not (case-folded). At
    # least one known concept was anchored.
    assert any(n.lower() in names for n in known)
    # Source ref anchored to the chunk.
    for c in prop.concepts:
        assert c.source_refs
        assert c.source_refs[0].chunk_id == "chk1"
        assert c.source_refs[0].physical_page == 55


def test_offline_derives_concept_from_title_when_no_known_match():
    router = ModelRouter(RouterConfig(live=False))
    agent = BookMapperAgent(router)
    sec = _section("s_new", "Generics 进阶", ("Generics 进阶",), 120)
    chunks = [_chunk("chk9", "这里讲一些全新的内容。", 120, ("Generics 进阶",))]
    prop = agent.map_section(sec, chunks, known_concept_names=["Variables"])
    # No known concept matched → title-derived concept.
    assert len(prop.concepts) == 1
    assert prop.concepts[0].name == "Generics 进阶"
    assert prop.concepts[0].source_refs[0].chunk_id == "chk9"


def test_offline_proposes_related_for_co_occurring_known_concepts():
    router = ModelRouter(RouterConfig(live=False))
    agent = BookMapperAgent(router)
    sec = _section("s1", "Strings", ("Strings",), 30)
    chunks = [_chunk("chk1", "equals 与 hashCode 必须一致。", 30, ("Strings",))]
    prop = agent.map_section(sec, chunks, known_concept_names=["equals", "hashCode"])
    rel_names = {(r.source_name, r.target_name) for r in prop.relations}
    assert any(r.relation == RelationType.RELATED for r in prop.relations)
    # The pair (equals, hashCode) co-occurs.
    assert ("equals", "hashCode") in rel_names or ("hashCode", "equals") in rel_names


def test_offline_difficulty_guess_from_keywords():
    router = ModelRouter(RouterConfig(live=False))
    agent = BookMapperAgent(router)
    sec = _section("s1", "契约", ("契约",), 1)
    chunks = [_chunk("chk1", "讨论 hashCode 的契约与底层原理。", 1, ("契约",))]
    prop = agent.map_section(sec, chunks, known_concept_names=["hashCode"])
    c = next(c for c in prop.concepts if c.name.lower() == "hashcode")
    assert c.difficulty == Difficulty.HARD


def test_offline_empty_chunks_returns_empty_proposal():
    router = ModelRouter(RouterConfig(live=False))
    agent = BookMapperAgent(router)
    sec = _section("s1", "X", ("X",), 1)
    prop = agent.map_section(sec, [], known_concept_names=["A"])
    assert prop.concepts == []
    assert prop.fallback is True


# --- live / coerced path (stubbed router) ---------------------------------


class _StubRouter(ModelRouter):
    """Returns a canned JSON response, simulating a live model."""

    def __init__(self, payload: dict):
        super().__init__(RouterConfig(live=False))
        self._payload = payload

    def complete(self, task, messages, *, output_schema=None, temperature=None, max_tokens=None):
        from bookmind.llm.schemas import ModelResult
        return ModelResult(
            ok=True, task=task, model="stub", content=json.dumps(self._payload),
            parsed_json=self._payload, prompt_version="test",
        )


def _stub_router_carrying_invalid_json_falls_back():
    class _BadRouter(ModelRouter):
        def __init__(self):
            super().__init__(RouterConfig(live=False))

        def complete(self, task, messages, *, output_schema=None, temperature=None, max_tokens=None):
            from bookmind.llm.schemas import ModelResult
            # ok but unparseable JSON content; parsed_json None.
            return ModelResult(ok=True, task=task, model="stub", content="not json",
                               parsed_json=None, prompt_version="test")
    return _BadRouter()


def test_live_path_coerces_model_json():
    import json
    payload = {
        "concepts": [
            {"name": "Generics", "description": "类型参数化", "importance": 0.8,
             "difficulty": "HARD", "prerequisites": ["Variables & primitive types"],
             "chunk_id": "chk1", "rationale": "core"},
        ],
        "relations": [
            {"source": "Generics", "target": "Variables & primitive types",
             "relation": "PREREQUISITE"},
        ],
    }
    router = _StubRouter(payload)
    agent = BookMapperAgent(router)
    sec = _section("s1", "Generics", ("Generics",), 100)
    chunks = [_chunk("chk1", "Generics 类型参数化。", 100, ("Generics",))]
    prop = agent.map_section(sec, chunks, known_concept_names=["Variables & primitive types"])
    assert prop.fallback is False
    assert len(prop.concepts) == 1
    c = prop.concepts[0]
    assert c.name == "Generics"
    assert c.difficulty == Difficulty.HARD
    assert c.importance == 0.8
    assert c.proposed_prerequisites == ["Variables & primitive types"]
    assert c.source_refs[0].chunk_id == "chk1"
    assert len(prop.relations) == 1
    assert prop.relations[0].relation == RelationType.PREREQUISITE


def test_live_path_drops_malformed_concept_entries():
    payload = {
        "concepts": [
            {"name": ""},  # dropped: empty name
            {"description": "no name"},  # dropped: no name
            {"name": "Valid", "importance": "not a number", "difficulty": "WEIRD"},
            "not a dict",  # dropped
        ],
        "relations": [{"source": "", "target": "x"}],  # dropped
    }
    router = _StubRouter(payload)
    agent = BookMapperAgent(router)
    sec = _section("s1", "S", ("S",), 1)
    chunks = [_chunk("chk1", "text", 1, ("S",))]
    prop = agent.map_section(sec, chunks)
    assert len(prop.concepts) == 1
    assert prop.concepts[0].name == "Valid"
    # bad importance → default 0.5; bad difficulty → MEDIUM
    assert prop.concepts[0].importance == 0.5
    assert prop.concepts[0].difficulty == Difficulty.MEDIUM


def test_model_unparseable_json_falls_back_to_offline():
    router = _stub_router_carrying_invalid_json_falls_back()
    agent = BookMapperAgent(router)
    sec = _section("s1", "Variables & primitive types", ("Variables & primitive types",), 41)
    chunks = [_chunk("chk1", "讲变量与基本类型。", 41, ("Variables & primitive types",))]
    prop = agent.map_section(sec, chunks, known_concept_names=["Variables & primitive types"])
    # Falls back to offline extractor (deterministic), still produces a proposal.
    assert prop.fallback is True
    assert len(prop.concepts) >= 1
