"""Regression tests for context rewriting in textbook Q&A."""

from bookmind.domain.models import ContentBlock, Message
from bookmind.services.conversation_orchestrator import _rewrite_followup


def _history(question: str) -> list[Message]:
    return [Message(
        message_id="m1", conversation_id="c1", role="user",
        content_blocks=[ContentBlock(type="text", text=question)],
    )]


def test_short_self_contained_question_does_not_inherit_previous_topic():
    assert _rewrite_followup("列表是什么？", _history("KMP算法是什么？")) == "列表是什么？"


def test_explicit_deictic_question_keeps_previous_topic_as_context():
    rewritten = _rewrite_followup("那它为什么更快？", _history("KMP算法是什么？"))
    assert "上一轮问题：KMP算法是什么？" in rewritten
    assert "本轮追问：那它为什么更快？" in rewritten
