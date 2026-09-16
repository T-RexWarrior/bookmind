"""Regression tests for context rewriting in textbook Q&A."""

from bookmind.domain.models import ContentBlock, Message
from bookmind.services.conversation_orchestrator import _bounded_conversation_context, _rewrite_followup


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


def test_polite_embedded_pronoun_keeps_previous_topic_as_context():
    rewritten = _rewrite_followup("告诉我他跟栈的区别", _history("队列有什么用？"))
    assert "上一轮问题：队列有什么用？" in rewritten
    assert "本轮追问：告诉我他跟栈的区别" in rewritten


def test_bounded_conversation_context_keeps_roles_and_omits_non_text_blocks():
    history = _history("KMP算法是什么？") + [Message(
        message_id="m2", conversation_id="c1", role="assistant",
        content_blocks=[
            ContentBlock(type="text", text="它用于字符串匹配。"),
            ContentBlock(type="citation", quote="不应进入摘要"),
        ],
    )]
    recap = _bounded_conversation_context(history)
    assert "学习者：KMP算法是什么？" in recap
    assert "助理：它用于字符串匹配。" in recap
    assert "不应进入摘要" not in recap
