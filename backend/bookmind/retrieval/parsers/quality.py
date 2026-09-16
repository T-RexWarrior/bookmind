"""Deterministic page-quality scoring for adaptive PDF parsing.

The score is intentionally language-aware without trusting the filename.  It
does not try to prove that extracted text is semantically correct; it catches
the common failure modes that make a text layer unsafe for retrieval: almost
empty pages, replacement/control characters, repeated glyph garbage and
implausibly fragmented output.
"""

from __future__ import annotations

import math
import re
from collections import Counter


# A recurring damaged ToUnicode map in Chinese teaching PDFs maps common
# glyphs (for example 的、确认、进行、记录) to unrelated but valid CJK code
# points.  Unicode-validity checks cannot see this failure.  These rare glyphs
# are a fingerprint of that map; requiring several per page keeps ordinary
# prose from being misclassified because of one legitimate uncommon word.
_BROKEN_CJK_FONT_GLYPHS = frozenset("癿丌乀讣迕尿弼刜觃弽讴刞枂极倚劣仹叏返绉")


def looks_like_broken_cjk_font_map(text: str) -> bool:
    visible = sum(not char.isspace() for char in (text or ""))
    if visible < 80:
        return False
    suspicious = sum(char in _BROKEN_CJK_FONT_GLYPHS for char in text)
    return suspicious >= 3 and suspicious / visible >= 0.001


def score_page_text(text: str, *, expected_text_page: bool = True) -> tuple[float, list[str]]:
    text = text or ""
    visible_chars = [char for char in text if not char.isspace()]
    visible = len(visible_chars)
    warnings: list[str] = []

    if visible == 0:
        return (0.0 if expected_text_page else 0.55), ["页面没有可用文字"]

    # Textbook pages normally contain enough text for extraction to be useful.
    # A short cover or divider page is not necessarily corrupt, so this signal
    # is deliberately capped rather than making the whole score zero.
    coverage = min(1.0, visible / 220.0)
    if visible < 40:
        warnings.append("页面文字很少")

    replacement = sum(char in {"\ufffd", "\x00"} for char in visible_chars)
    controls = sum(ord(char) < 32 for char in visible_chars)
    invalid_ratio = (replacement + controls) / visible
    validity = max(0.0, 1.0 - invalid_ratio * 25.0)
    if invalid_ratio > 0.005:
        warnings.append("包含异常或替换字符")

    # A broken font map often yields one glyph repeated hundreds of times.
    counts = Counter(visible_chars)
    dominant_ratio = counts.most_common(1)[0][1] / visible
    diversity = min(1.0, len(counts) / max(12.0, math.sqrt(visible) * 1.7))
    repetition = min(1.0, max(0.0, (0.55 - dominant_ratio) / 0.45))
    if dominant_ratio > 0.35:
        warnings.append("字符异常重复")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    unique_line_ratio = len(set(lines)) / max(1, len(lines))
    if len(lines) >= 8 and unique_line_ratio < 0.45:
        warnings.append("页面存在大量重复行")

    # Both Chinese and Latin textbook prose are valid.  Penalise text that is
    # mostly punctuation/private-use glyphs instead of assuming one language.
    language_chars = sum(
        char.isalnum() or "\u4e00" <= char <= "\u9fff"
        for char in visible_chars
    )
    language_ratio = language_chars / visible
    plausibility = min(1.0, language_ratio / 0.70)
    if language_ratio < 0.45:
        warnings.append("可识别的中英文字符比例偏低")

    # Excessive isolated one-character lines are typical of broken reading
    # order or vertical text being flattened incorrectly.
    fragments = sum(len(re.sub(r"\s+", "", line)) <= 1 for line in lines)
    fragment_ratio = fragments / max(1, len(lines))
    continuity = max(0.0, 1.0 - fragment_ratio)
    if len(lines) >= 8 and fragment_ratio > 0.45:
        warnings.append("文本行过度碎片化")

    score = (
        0.22 * coverage
        + 0.25 * validity
        + 0.15 * diversity
        + 0.10 * repetition
        + 0.10 * unique_line_ratio
        + 0.13 * plausibility
        + 0.05 * continuity
    )
    if looks_like_broken_cjk_font_map(text):
        # Force the page into the adaptive parser's OCR-review band.  This is
        # intentionally a late penalty: all other diagnostics remain useful
        # in the UI and for choosing between native and OCR candidates.
        score -= 0.28
        warnings.append("中文字体映射疑似损坏")
    return round(max(0.0, min(1.0, score)), 4), warnings


def quality_label(score: float) -> str:
    if score >= 0.80:
        return "GOOD"
    if score >= 0.45:
        return "WARN"
    return "BAD"


def summarize_page_quality(pages) -> dict:
    scores = [page.quality_score for page in pages if page.quality_score is not None]
    labels = Counter(page.quality_label or "UNKNOWN" for page in pages)
    return {
        "average": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "good_pages": labels.get("GOOD", 0),
        "warning_pages": labels.get("WARN", 0),
        "bad_pages": labels.get("BAD", 0),
        "unknown_pages": labels.get("UNKNOWN", 0),
        "total_pages": len(pages),
    }


def detect_printed_page(text: str, physical_page: int) -> str | None:
    """Conservatively detect a standalone printed page number near an edge."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    candidates = [*lines[:2], *lines[-2:]]
    for candidate in candidates:
        match = re.fullmatch(r"[-—–·\s]*(\d{1,4}|[ivxlcdmIVXLCDM]{1,8})[-—–·\s]*", candidate)
        if not match:
            continue
        value = match.group(1)
        if value.isdigit() and int(value) > physical_page + 200:
            continue
        return value
    return None


__all__ = [
    "detect_printed_page", "looks_like_broken_cjk_font_map", "score_page_text",
    "quality_label", "summarize_page_quality",
]
