"""Offline demo corpus — AI Exam Assistant style (OPEN_SOURCE_REFERENCES §8).

A pre-parsed Java/OOP textbook fragment, its chunks, the gold concept skeleton,
and a couple of seeded learner states — all built deterministically with no
network, no PDF and no live model. This is the "可完全离线运行的 seed demo"
the spec requires: the full Q&A + state loop runs offline for the competition
demo and the CI smoke test.

Real-model responses are an opt-in enhancement, not a single point of dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import ConceptSource, Difficulty, Level, LevelStatus
from ..domain.models import Concept, LearnerConceptState, LevelRecord
from ..domain.source_ref import SourceRef
from ..retrieval.chunk import DocumentChunk
from ..retrieval.parsed_document import Block, ParsedDocument, Section
from ..agents.concept_skeleton import build_skeleton


DEMO_BOOK_ID = "demo_java_core"
DEMO_DOCUMENT_ID = "demo_doc_java"


# A hand-written structural textbook: 4 sections covering the core Java/OOP
# scenario from PRODUCT_SPEC §4.2 (variables/references, ==/equals,
# equals/hashCode, polymorphism).
_DEMO_BLOCKS: list[tuple[str, str, str, int]] = [
    # (block_id, text, section_path, physical_page)
    ("b1", "3 变量、引用与对象", "3 变量、引用与对象", 41),
    ("b2", "变量是存储位置的名称，每个变量都有一个类型。", "3 变量、引用与对象", 41),
    ("b3", "基本类型直接存储值，引用类型存储的是对象的地址。", "3 变量、引用与对象", 42),
    ("b4", "引用变量保存的是对象的地址，而不是对象本身。", "3 变量、引用与对象", 42),

    ("b5", "4 == 与 equals", "4 == 与 equals", 55),
    ("b6", "== 运算符比较两个引用是否指向同一个对象，即地址是否相同。", "4 == 与 equals", 55),
    ("b7", "equals 方法用于比较两个对象的内容是否相等。", "4 == 与 equals", 56),
    ("b8", "对于 String，new 出来的两个内容相同的对象用 == 比较为 false，用 equals 比较为 true。", "4 == 与 equals", 56),

    ("b9", "5 equals 与 hashCode 的契约", "5 equals 与 hashCode 的契约", 70),
    ("b10", "重写 equals 方法时必须同时重写 hashCode 方法。", "5 equals 与 hashCode 的契约", 70),
    ("b11", "如果两个对象 equals 为 true，它们的 hashCode 必须相同，否则会破坏 HashMap 等基于哈希的集合。", "5 equals 与 hashCode 的契约", 71),

    ("b12", "6 继承与多态", "6 继承与多态", 88),
    ("b13", "继承使用 extends 关键字，子类获得父类的非私有成员。", "6 继承与多态", 88),
    ("b14", "多态指同一调用在不同对象上表现出不同行为，依赖动态分派。", "6 继承与多态", 89),
    ("b15", "动态分派在运行时根据对象的实际类型决定调用哪个重写方法。", "6 继承与多态", 89),

    # --- Interfaces & abstract classes (Phase 3 extension) ---
    ("b16", "7 接口与抽象类", "7 接口与抽象类", 100),
    ("b17", "接口是方法的契约，类通过 implements 实现接口。", "7 接口与抽象类", 100),
    ("b18", "抽象类用 abstract 修饰，不能实例化，可包含抽象方法与具体方法。", "7 接口与抽象类", 101),
    ("b19", "default 方法允许接口提供默认实现，实现类可以选择重写。", "7 接口与抽象类", 101),

    # --- Collections framework (Phase 3 extension) ---
    ("b20", "8 集合框架", "8 集合框架", 120),
    ("b21", "List 是有序集合，允许重复元素，常用 ArrayList 实现。", "8 集合框架", 120),
    ("b22", "Set 是不允许重复元素的集合，HashSet 基于哈希表实现。", "8 集合框架", 121),
    ("b23", "Map 是键值对映射，HashMap 要求键正确实现 hashCode 与 equals。", "8 集合框架", 121),
    ("b24", "迭代器用于遍历集合，for-each 循环依赖 Iterable 接口。", "8 集合框架", 122),

    # --- Exceptions (Phase 3 extension) ---
    ("b25", "9 异常处理", "9 异常处理", 140),
    ("b26", "异常是程序运行时出现的错误对象，分为受检异常与非受检异常。", "9 异常处理", 140),
    ("b27", "try-catch 语句捕获异常，finally 块无论是否异常都会执行。", "9 异常处理", 141),
    ("b28", "throws 声明方法可能抛出的受检异常，throw 主动抛出异常对象。", "9 异常处理", 141),

    # --- Generics (Phase 3 extension) ---
    ("b29", "10 泛型", "10 泛型", 160),
    ("b30", "泛型是类型参数化机制，使集合与类能安全地处理多种类型。", "10 泛型", 160),
    ("b31", "类型擦除指泛型信息在编译后被移除，运行时无法获得泛型类型。", "10 泛型", 161),
    ("b32", "通配符 ? extends T 表示上界，? super T 表示下界。", "10 泛型", 161),

    # --- Nested classes (Phase 3 extension) ---
    ("b33", "11 嵌套类与内部类", "11 嵌套类与内部类", 180),
    ("b34", "内部类是定义在另一个类内部的类，能访问外部类的成员。", "11 嵌套类与内部类", 180),
    ("b35", "静态嵌套类用 static 修饰，不依赖外部类实例。", "11 嵌套类与内部类", 181),
    ("b36", "匿名内部类常用于即时实现接口或继承类。", "11 嵌套类与内部类", 181),

    # --- Lambda & functional interfaces (Phase 3 extension) ---
    ("b37", "12 Lambda 与函数式接口", "12 Lambda 与函数式接口", 200),
    ("b38", "函数式接口是只有一个抽象方法的接口，可用 @FunctionalInterface 标注。", "12 Lambda 与函数式接口", 200),
    ("b39", "Lambda 表达式是匿名函数的简洁写法，依赖函数式接口类型推断。", "12 Lambda 与函数式接口", 201),
    ("b40", "方法引用是 Lambda 的进一步简写，形如 类名::方法名。", "12 Lambda 与函数式接口", 201),

    # --- Streams (Phase 3 extension) ---
    ("b41", "13 Stream API", "13 Stream API", 220),
    ("b42", "流是对集合的声明式序列操作管道，支持过滤、映射与归约。", "13 Stream API", 220),
    ("b43", "中间操作如 filter 与 map 是惰性的，终端操作如 collect 触发执行。", "13 Stream API", 221),
    ("b44", "流不修改数据源，collect 将结果收集到新的集合。", "13 Stream API", 221),
]


@dataclass
class DemoCorpus:
    """A self-contained, offline-runnable BookMind demo dataset."""

    book_id: str = DEMO_BOOK_ID
    document_id: str = DEMO_DOCUMENT_ID
    parsed_document: ParsedDocument = field(default_factory=lambda: _build_parsed_doc())
    chunks: list[DocumentChunk] = field(default_factory=lambda: _build_chunks())
    concepts: list[Concept] = field(default_factory=lambda: build_skeleton(DEMO_BOOK_ID))

    def seed_concepts_into(self, repo) -> None:
        for c in self.concepts:
            repo.add_concept(c)

    def seed_chunks_into(self, repo) -> None:
        repo.add_chunks(self.book_id, self.chunks)

    def build_retriever(self, router):
        """Index the demo chunks into a fresh HybridRetriever (offline router)."""
        from ..retrieval.bm25 import BM25Index
        from ..retrieval.vector import VectorStore
        from ..retrieval.fusion import HybridRetriever
        ret = HybridRetriever(BM25Index(), VectorStore(), router, rerank_enabled=False)
        ret.index_chunks(self.chunks)
        return ret


def _build_parsed_doc() -> ParsedDocument:
    from ..retrieval.parsed_document import Page

    blocks = []
    for i, (bid, text, section, page) in enumerate(_DEMO_BLOCKS):
        is_heading = text == section
        blocks.append(Block(
            block_id=bid, block_type="heading" if is_heading else "text",
            text=text, physical_page=page, reading_order=i, section_path=(section,),
        ))

    sections: list[Section] = []
    seen: set[str] = set()
    for bid, text, section, page in _DEMO_BLOCKS:
        if section not in seen:
            seen.add(section)
            sections.append(Section(
                section_id=f"sec-{len(sections)+1}", title=section,
                section_path=(section,), physical_page=page,
                block_ids=[b for (b, _, s, _) in _DEMO_BLOCKS if s == section],
            ))

    pages_map: dict[int, list[str]] = {}
    for bid, text, section, page in _DEMO_BLOCKS:
        pages_map.setdefault(page, []).append(bid)
    pages = [Page(physical_page=p, block_ids=pages_map[p]) for p in sorted(pages_map)]

    return ParsedDocument(
        document_id=DEMO_DOCUMENT_ID, source_file="java_core.pdf",
        source_hash="demo_hash", parser="manual", parser_version="demo_v1",
        pages=pages, sections=sections, blocks=blocks,
    )


def _build_chunks() -> list[DocumentChunk]:
    """One chunk per non-heading block, carrying full provenance."""
    chunks = []
    for i, (bid, text, section, page) in enumerate(_DEMO_BLOCKS):
        if text == section:
            continue  # skip headings as chunks
        ref = SourceRef(
            document_id=DEMO_DOCUMENT_ID, chunk_id=f"demo-chk-{i}",
            block_id=bid, physical_page=page, section_path=(section,),
        )
        chunks.append(DocumentChunk(
            chunk_id=f"demo-chk-{i}", book_id=DEMO_BOOK_ID,
            document_id=DEMO_DOCUMENT_ID, section_path=(section,),
            content=text, source_ref=ref, block_ids=[bid],
            parser_version="demo_v1",
        ))
    return chunks


# --- seeded learner states ------------------------------------------------

def demo_learner_states(project_id: str = "demo_project") -> list[LearnerConceptState]:
    """Three learner states with different gaps (OPEN_SOURCE_REFERENCES §8:
    "三个具有不同缺口的 learner state")."""
    return [
        # Learner A: solid on variables, unverified on equals/hashCode.
        _state(project_id, "c_variable", current=Level.L2, l1=LevelStatus.VERIFIED, l2=LevelStatus.VERIFIED),
        _state(project_id, "c_reference", current=Level.L1, l1=LevelStatus.VERIFIED),
        _state(project_id, "c_hashcode", current=Level.L0, l1=LevelStatus.UNVERIFIED),
        # Learner B: expired on == vs equals (due for review).
        _state(project_id, "c_reference_equality", current=Level.L0, l1=LevelStatus.EXPIRED),
    ]


def _state(project_id, concept_id, *, current=Level.L0, highest=Level.L0,
           l1=LevelStatus.UNVERIFIED, l2=LevelStatus.UNVERIFIED) -> LearnerConceptState:
    s = LearnerConceptState(project_id=project_id, concept_id=concept_id,
                            current_verified_level=current, highest_ever_level=highest or current)
    s.levels[Level.L1.value] = LevelRecord(status=l1)
    s.levels[Level.L2.value] = LevelRecord(status=l2)
    return s
