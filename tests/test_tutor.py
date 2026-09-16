"""L1/L2 tests: Tutor Agent — ARCHITECTURE.md §3.2, §4.2.

Covers grounded answering, citation validation, the regenerate-once-then-reject
path, and the conservative offline fallback. Uses an injected ModelRouter so no
network is needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from bookmind.agents.tutor import TutorAgent, _parse_answer, _repair_citation_quotes, _safe_excerpt
from bookmind.domain.enums import Level
from bookmind.domain.source_ref import SourceRef
from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.retrieval.chunk import DocumentChunk
from bookmind.retrieval.citation import CitationValidator
from bookmind.retrieval.fusion import RetrievalHit


def _chunk(cid, content, page=1, book="b1"):
    return DocumentChunk(
        chunk_id=cid, book_id=book, document_id="d1", content=content,
        source_ref=SourceRef(document_id="d1", chunk_id=cid, block_id=cid, physical_page=page),
    )


def _hit(cid, content, page=1):
    return RetrievalHit(chunk=_chunk(cid, content, page))


# --- a controllable fake router ------------------------------------------

@dataclass
class FakeModelResult:
    ok: bool = True
    content: str = ""
    fallback: bool = False
    error: str = ""


class FakeRouter(ModelRouter):
    """A router that returns canned chat contents in sequence."""

    def __init__(self, responses: list[str], fail: bool = False):
        super().__init__(RouterConfig(live=False))
        self._responses = list(responses)
        self._fail = fail
        self.calls: list[dict] = []

    def complete(self, task, messages, *, output_schema=None, temperature=None, max_tokens=None):
        self.calls.append({"task": task, "messages": messages})
        if self._fail:
            return FakeModelResult(ok=False, content="", fallback=True, error="model down")
        if not self._responses:
            return FakeModelResult(ok=False, content="", fallback=True, error="no canned response")
        return FakeModelResult(ok=True, content=self._responses.pop(0))


def _validator(chunks, books=None):
    by_id = {c.chunk_id: c for c in chunks}
    return CitationValidator(by_id, books or {c.book_id for c in chunks})


# --- grounded answer with valid citations --------------------------------

def test_answer_with_valid_citations():
    chunks = [_chunk("c1", "equals 方法比较两个对象的内容是否相等。", page=42)]
    router = FakeRouter([
        '== 比较引用，equals 比较内容。\n[{"chunk_id":"c1","quote":"equals 方法比较两个对象的内容是否相等。","page":"42"}]'
    ])
    tutor = TutorAgent(router, _validator(chunks))
    ans = tutor.answer("== 和 equals 区别", _hit("c1", chunks[0].content, 42).__class__ and [_hit("c1", chunks[0].content, 42)])
    assert ans.grounded is True
    assert ans.citations[0]["chunk_id"] == "c1"
    assert "equals" in ans.text


def test_answer_no_chunks_rejects_gracefully():
    router = FakeRouter([])
    tutor = TutorAgent(router, _validator([]))
    ans = tutor.answer("anything", [])
    assert ans.grounded is False
    assert "未找到" in ans.text


# --- citation failure → regenerate → reject ------------------------------

def test_answer_regenerates_then_succeeds():
    chunks = [_chunk("c1", "引用变量保存对象的地址，而非对象本身。")]
    # First answer cites a non-existent chunk; second cites correctly.
    bad = '[{"chunk_id":"c99","quote":"...","page":"1"}]'
    good = '引用是地址。\n[{"chunk_id":"c1","quote":"引用变量保存对象的地址，而非对象本身。","page":"1"}]'
    router = FakeRouter([f"答案A\n{bad}", good])
    tutor = TutorAgent(router, _validator(chunks))
    ans = tutor.answer("引用是什么", [_hit("c1", chunks[0].content)])
    assert ans.grounded is True
    assert ans.regenerated is True  # succeeded on the second attempt


def test_answer_rejects_after_two_failures():
    chunks = [_chunk("c1", "引用变量保存对象的地址。")]
    bad1 = '[{"chunk_id":"c99","quote":"x","page":"1"}]'
    bad2 = '[{"chunk_id":"c1","quote":"这句原文里没有","page":"1"}]'
    router = FakeRouter([f"答案A\n{bad1}", f"答案B\n{bad2}"])
    tutor = TutorAgent(router, _validator(chunks))
    ans = tutor.answer("引用是什么", [_hit("c1", chunks[0].content)])
    assert ans.grounded is False
    assert "未找到" in ans.reason or "failed" in ans.reason
    assert "无法" in ans.text or "不给出" in ans.text
    assert len(router.calls) == 2  # tried twice


# --- model fallback ------------------------------------------------------

def test_answer_falls_back_when_model_down():
    chunks = [_chunk("c1", "变量是存储位置的名称。", page=10)]
    router = FakeRouter([], fail=True)
    tutor = TutorAgent(router, _validator(chunks))
    ans = tutor.answer("变量是什么", [_hit("c1", chunks[0].content, 10)])
    assert ans.fallback is True
    assert ans.grounded is False
    assert ans.citations == []  # never expose an excerpt as a generated answer


def test_model_fallback_never_exposes_parser_code_debris():
    assert _safe_excerpt("class Fib { int prev() { return 1; } }") == ""
    assert _safe_excerpt("A variable names a storage location used while a program runs.").startswith("A variable")


def test_model_fallback_never_exposes_parser_code_debris():
    assert _safe_excerpt("class Fib { int prev() { return 1; } }") == ""
    assert _safe_excerpt("A variable names a storage location used while a program runs.").startswith("A variable")


def test_answer_never_drops_citation_but_keeps_claim():
    """PRODUCT_SPEC §8: cannot just delete a citation and keep the claim.
    After two failed validations the whole answer is rejected, not partially."""
    chunks = [_chunk("c1", "正确内容")]
    # An answer whose only citation is invalid.
    bad = '某个论断\n[{"chunk_id":"c99","quote":"x","page":"1"}]'
    router = FakeRouter([bad, bad])
    tutor = TutorAgent(router, _validator(chunks))
    ans = tutor.answer("q", [_hit("c1", chunks[0].content)])
    assert ans.grounded is False
    # The unsupported claim "某个论断" must NOT be returned as a grounded answer.
    assert "某个论断" not in ans.text


def test_repair_citation_quote_resolves_tiny_punctuation_drift_to_source():
    chunk = _chunk("c1", "算法是一个指令序列，\n用于解决信息处理问题。")
    citations = [{
        "chunk_id": "c1",
        "quote": "算法是一个指令序列,用于解决信息处理问题。",
    }]

    repaired = _repair_citation_quotes(citations, [chunk])

    assert repaired[0]["quote"] in chunk.content
    assert "，" in repaired[0]["quote"]


def test_repair_citation_quote_does_not_accept_material_paraphrase():
    chunk = _chunk("c1", "算法是一个指令序列，用于解决信息处理问题。")
    changed = "算法是一套程序代码，可以解决所有计算问题。"

    repaired = _repair_citation_quotes(
        [{"chunk_id": "c1", "quote": changed}], [chunk],
    )

    assert repaired[0]["quote"] == changed


# --- _parse_answer robustness -------------------------------------------

def test_parse_answer_array_form():
    prose, cites = _parse_answer('答案是A。\n[{"chunk_id":"c1","quote":"q","page":"1"}]')
    assert prose == "答案是A。"
    assert cites == [{"chunk_id": "c1", "quote": "q", "page": "1"}]


def test_parse_answer_single_object_form():
    prose, cites = _parse_answer('答案是A。\n{"chunk_id":"c1","quote":"q","page":"1"}')
    assert "答案是A" in prose
    assert cites == [{"chunk_id": "c1", "quote": "q", "page": "1"}]


def test_parse_answer_no_json_returns_prose():
    prose, cites = _parse_answer("这是一个没有引用的回答。")
    assert cites == []
    assert "没有引用" in prose


def test_parse_answer_citation_with_braces_in_quote():
    # quote contains a { which must not break the balance scan.
    txt = '答案。\n[{"chunk_id":"c1","quote":"map{key}=val","page":"1"}]'
    prose, cites = _parse_answer(txt)
    assert cites[0]["quote"] == "map{key}=val"


def test_parse_answer_strips_trailing_fenced_json():
    prose, cites = _parse_answer(
        '不支持清除操作 clear。\n```json\n[{"chunk_id":"c1","quote":"不支持清除操作 clear","page":"486"}]\n```'
    )
    assert prose == "不支持清除操作 clear。"
    assert cites == [{"chunk_id": "c1", "quote": "不支持清除操作 clear", "page": "486"}]


def test_parse_answer_accepts_structured_json_object():
    prose, cites = _parse_answer(
        '{"answer":"不支持 clear。","citations":[{"chunk_id":"c1","quote":"clear","page":"486"}]}'
    )
    assert prose == "不支持 clear。"
    assert cites[0]["chunk_id"] == "c1"
