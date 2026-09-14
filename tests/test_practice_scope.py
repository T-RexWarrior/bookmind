"""Practice queues should contain teachable leaf sections only."""

from bookmind.domain.models import Concept
from bookmind.domain.source_ref import SourceRef
from bookmind.services.task_service import _is_practice_worthy


def _concept(name: str, path: tuple[str, ...], *, chapter: str = "") -> Concept:
    return Concept(
        concept_id=f"concept-{name}",
        book_id="book_real",
        name=name,
        chapter=chapter,
        source_refs=[SourceRef(
            document_id="doc-1",
            chunk_id="book_real:chunk-1",
            physical_page=1,
            section_path=path,
        )],
    )


def test_book_title_inheriting_preface_reference_is_not_practice_worthy():
    concept = _concept(
        "数据结构（C++语言版）",
        ("数据结构（C++语言版）", "序"),
        chapter="数据结构（C++语言版）",
    )
    assert not _is_practice_worthy(concept)


def test_real_leaf_section_is_practice_worthy():
    concept = _concept(
        "§1.2 复杂度度量",
        ("第1章 绪论", "§1.2 复杂度度量"),
        chapter="第1章 绪论",
    )
    assert _is_practice_worthy(concept)


def test_front_matter_is_excluded_even_with_spacing():
    concept = _concept("致    谢", ("数据结构", "致    谢"))
    assert not _is_practice_worthy(concept)
