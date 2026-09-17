"""Question-topic resolution must not inherit document retrieval mistakes."""

import json

from bookmind.domain.models import Concept
from bookmind.domain.source_ref import SourceRef
from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.services.concept_resolution import ConceptResolver


class _Repo:
    def __init__(self, concepts):
        self._concepts = concepts

    def allowed_book_ids(self, _project_id):
        return {"book"}

    def concepts_for_book(self, _book_id):
        return self._concepts

    def chunks_for_project(self, _project_id):
        return []


def _concept(concept_id: str, name: str, leaf: str, page: int) -> Concept:
    return Concept(
        concept_id=concept_id, book_id="book", name=name,
        chapter="第4章 栈与队列", section=leaf,
        source_refs=[SourceRef(
            document_id="doc", chunk_id=f"chunk-{concept_id}", physical_page=page,
            section_path=("第4章 栈与队列", leaf),
        )],
    )


def _resolver(*concepts: Concept) -> ConceptResolver:
    return ConceptResolver(_Repo(list(concepts)), ModelRouter(RouterConfig(live=False)))


def test_single_character_leaf_alias_resolves_stack_not_a_retrieved_list():
    stack = _concept("stack", "栈与队列", "§4.1 栈", 108)
    listing = Concept(
        concept_id="list", book_id="book", name="列表", chapter="第3章 列表", section="§3.1 列表",
        source_refs=[SourceRef(document_id="doc", chunk_id="chunk-list", physical_page=87,
                               section_path=("第3章 列表", "§3.1 列表"))],
    )
    resolved = _resolver(listing, stack).resolve(project_id="p", question="栈是什么")
    assert [item.concept_id for item in resolved] == ["stack"]
    assert resolved[0].chunk_ids == ("chunk-stack",)


def test_comparison_can_resolve_two_explicit_leaf_concepts():
    stack = _concept("stack", "栈", "§4.1 栈", 108)
    queue = _concept("queue", "队列", "§4.5 队列", 127)
    resolved = _resolver(stack, queue).resolve(project_id="p", question="栈与队列的区别是什么")
    assert {item.concept_id for item in resolved} == {"stack", "queue"}


def test_comparison_is_not_capped_at_two_explicit_subjects():
    stack = _concept("stack", "栈", "§4.1 栈", 108)
    queue = _concept("queue", "队列", "§4.5 队列", 127)
    tree = _concept("tree", "二叉树", "§5.1 二叉树", 180)
    resolved = _resolver(stack, queue, tree).resolve(
        project_id="p", question="栈、队列和二叉树有什么区别？",
    )
    assert {item.concept_id for item in resolved} == {"stack", "queue", "tree"}


def test_resolved_graph_anchors_survive_lazy_chunk_restore_after_restart():
    stack = _concept("stack", "栈", "§4.1 栈", 108)
    resolver = _resolver(stack)
    resolved = resolver.resolve(project_id="p", question="栈是什么？")
    # Before BookQA restores its persisted index, the repository's in-memory
    # chunk list is empty. The exact graph anchor must still reach BookQA.
    assert resolver.evidence_chunk_ids(project_id="p", subjects=resolved) == ["chunk-stack"]


def test_unrelated_question_is_not_assigned_from_nearby_concepts():
    stack = _concept("stack", "栈与队列", "§4.1 栈", 108)
    listing = _concept("list", "列表", "§3.1 列表", 87)
    assert _resolver(stack, listing).resolve(project_id="p", question="二叉树如何遍历") == []


def test_queue_operations_are_aliases_of_the_queue_learning_unit():
    queue = _concept("queue", "队列", "§4.5 队列", 127)
    assert [item.concept_id for item in _resolver(queue).resolve(project_id="p", question="入队怎么入？")] == ["queue"]
    assert [item.concept_id for item in _resolver(queue).resolve(project_id="p", question="出队怎么出？")] == ["queue"]


def test_contextual_followup_returns_resolved_concepts_not_candidate_objects(monkeypatch):
    queue = _concept("queue", "队列", "§4.5 队列", 127)
    monkeypatch.setenv("TEST_TRACE_KEY", "test-key")

    def fake_http(_url, _payload, _key, _timeout):
        body = {"choices": [{"message": {"content": json.dumps({
            "relation": "FOLLOW_UP", "concept_ids": ["queue"], "confidence": 0.95,
        })}}], "usage": {"total_tokens": 12}}
        return 200, json.dumps(body)

    resolver = ConceptResolver(
        _Repo([queue]), ModelRouter(RouterConfig(live=True, api_key_env="TEST_TRACE_KEY"), http=fake_http),
    )
    result = resolver.resolve_contextual_followup(
        project_id="p", question="给个关于它的代码示例", conversation_context="学习者：队列有什么用？",
    )
    assert result.relation == "FOLLOW_UP"
    assert [item.concept_id for item in result.subjects] == ["queue"]


def test_legacy_cross_section_topic_never_becomes_a_retrieval_or_state_unit():
    broad_tree = Concept(
        concept_id="legacy-tree", book_id="book", name="二叉树",
        chapter="第5章 二叉树", section="第5章 二叉树",
        source_refs=[
            SourceRef(document_id="doc", chunk_id="chapter", physical_page=131,
                      section_path=("第5章 二叉树",)),
            SourceRef(document_id="doc", chunk_id="implementation", physical_page=139,
                      section_path=("第5章 二叉树", "§5.3 二叉树的实现")),
        ],
    )
    intro = _concept("tree-intro", "§5.1 二叉树及其表示", "§5.1 二叉树及其表示", 132)
    implementation = _concept("tree-impl", "§5.3 二叉树的实现", "§5.3 二叉树的实现", 139)
    resolved = _resolver(broad_tree, intro, implementation).resolve(
        project_id="p", question="二叉树在什么地方有实际应用？",
    )
    assert [item.concept_id for item in resolved] == ["tree-intro"]
