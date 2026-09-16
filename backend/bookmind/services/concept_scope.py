"""Shared policy for deciding which graph nodes are real learning concepts.

PDF outlines contain navigation and publishing matter as well as teachable
content.  Keeping this decision in one place prevents the learning profile,
practice queue and source outline from disagreeing about entries such as a
preface, table of contents or edition note.
"""

from __future__ import annotations

import re
from collections.abc import Sequence


_NON_LEARNING_SECTIONS = {
    "序", "丛书序", "译者序", "作者序", "前言", "序言", "导言",
    "第1版前言", "第2版前言", "第3版前言",
    "第1版说明", "第2版说明", "第3版说明",
    "致谢", "鸣谢", "简要目录", "详细目录", "目录", "目次",
    "教学计划编排方案建议", "教学建议", "阅读指南", "使用说明",
    "参考文献", "算法索引", "代码索引", "关键词索引", "索引",
    "版权页", "版权信息", "出版说明", "作者简介", "内容简介",
    "封面", "扉页", "书名页",
    "preface", "foreword", "acknowledgments", "acknowledgements",
    "contents", "tableofcontents", "bibliography", "references", "index",
    "copyright", "editionnote",
}


def normalise_section_name(value: str) -> str:
    """Normalise a heading for conservative navigation/noise comparisons."""
    compact = "".join((value or "").split()).casefold().lstrip("§*")
    return re.sub(r"[：:，,。.!！?？·•—–_\-]+$", "", compact)


_NORMALISED_EXCLUSIONS = {normalise_section_name(item) for item in _NON_LEARNING_SECTIONS}


def is_non_learning_label(value: str) -> bool:
    """Return true only for explicit publishing/navigation labels."""
    label = normalise_section_name(value)
    if not label or label in _NORMALISED_EXCLUSIONS:
        return True
    if "目录" in label or label.endswith("索引"):
        return True
    if re.fullmatch(r"第[一二三四五六七八九十百\d]+版(?:前言|说明)", label):
        return True
    return False


def is_learning_section(
    title: str,
    section_path: Sequence[str] | None,
    *,
    book_title: str = "",
    require_leaf: bool = True,
) -> bool:
    """Classify a parsed section before it becomes a graph proposal.

    Nested textbook outlines use a one-element path for chapter/navigation
    headings and a deeper path for teachable leaf sections.  Flat documents
    can opt out of the leaf requirement while still receiving the explicit
    publishing-matter filter.
    """
    path = tuple(item for item in (section_path or ()) if item)
    label = title or (path[-1] if path else "")
    if is_non_learning_label(label):
        return False
    if book_title and normalise_section_name(label) == normalise_section_name(book_title):
        return False
    if path and normalise_section_name(path[0]).startswith("附录"):
        return False
    if require_leaf and len(path) < 2:
        return False
    return True


def is_learning_concept(concept) -> bool:
    """Return whether a persisted graph node should enter learner workflows.

    A valid node must be anchored inside a teachable leaf section.  Its own
    name need not equal the section title: definition extraction can discover
    finer concepts inside an already recognised section.
    """
    if getattr(concept, "book_id", "") == "demo_java_core":
        return True
    name = getattr(concept, "name", "") or ""
    if is_non_learning_label(name):
        return False
    chapter = getattr(concept, "chapter", "") or ""
    normalised_chapter = normalise_section_name(chapter)
    if normalised_chapter.startswith("附录"):
        return False
    chapter_topic = re.sub(
        r"^第[一二三四五六七八九十百\d]+章", "", normalised_chapter,
    )
    # Chapter proposals often inherit every descendant reference during graph
    # merging.  Do not mistake that inherited leaf anchor for a fine-grained
    # learning concept.
    if chapter_topic and normalise_section_name(name) == chapter_topic:
        return False
    refs = getattr(concept, "source_refs", ()) or ()
    saw_section_path = False
    for ref in refs:
        path = tuple(getattr(ref, "section_path", ()) or ())
        saw_section_path = saw_section_path or bool(path)
        if len(path) < 2:
            continue
        if is_learning_section(path[-1], path, require_leaf=True):
            return True
    # Hand-authored/imported concepts may not carry section paths.  Preserve
    # them unless their label was explicitly classified as publishing matter.
    # Parsed textbook headings do carry paths, so a one-level cover/chapter
    # node remains excluded from learner state and practice.
    return not saw_section_path


__all__ = [
    "is_learning_concept",
    "is_learning_section",
    "is_non_learning_label",
    "normalise_section_name",
]
