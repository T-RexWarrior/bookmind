"""Probe answer classifier — LEARNING_MODEL.md §8, §9.

A diagnostic probe discriminates between the ``competing_hypotheses`` of one
:class:`~bookmind.agents.bug_library.BugEntry`. Given a learner's answer, the
classifier maps it to the *single* best-matching hypothesis (or None). This is
what lets one probe write FOR evidence for one hypothesis and AGAINST evidence
for the others — so mutual exclusion emerges at the source rather than only
being enforced after the fact.

Design (LEARNING_MODEL §9): the classifier must be explainable and
deterministic-first. It does NOT produce a probability. Offline it uses
keyword/pattern matching against ``BugEntry.expected_patterns`` and
``likely_wrong_answers``; a live LLM path may refine the pick but is constrained
to return one of the known hypothesis keys or null.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...agents.bug_library import BugEntry


def _router_live(router) -> bool:
    """True iff the router is configured for live network calls.

    The flag lives on ``router.cfg.live`` (not a top-level ``router.live``
    attribute), so a plain ``getattr(router, 'live', False)`` would always be
    False — which would silently force the deterministic path even when an API
    key is configured.
    """
    cfg = getattr(router, "cfg", None)
    return bool(getattr(cfg, "live", False))


@dataclass
class ClassificationResult:
    """The outcome of classifying one probe answer."""

    best_hypothesis: str | None  # a hypothesis key (the part before ":"), or None
    scores: dict[str, int] = field(default_factory=dict)
    method: str = "keyword"  # "keyword" or "llm"
    reason: str = ""


def hypothesis_key(hypothesis: str) -> str:
    """Extract the key prefix before ':' from a competing_hypotheses entry."""
    return hypothesis.split(":", 1)[0].strip()


def _keywords_from_text(text: str) -> list[str]:
    """Extract content-bearing tokens from a pattern/wrong-answer description.

    We keep quoted phrases, alphanumerics (incl. CJK runs), numbers and the
    distinctive technical terms (==, equals, hashCode, …). Stop-words and very
    short tokens are dropped. This stays dependency-free.
    """
    # Pull out quoted phrases first — they are the most discriminative.
    quoted = re.findall(r'"([^"]+)"|\'([^\']+)\'|“([^”]+)”|‘([^’]+)’', text)
    phrases: list[str] = []
    for groups in quoted:
        for g in groups:
            if g:
                phrases.append(g.lower())
    # Alphanumeric/CJK tokens of length >= 2, plus symbolic operators.
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[一-鿿]{2,}|==|>=|<=|!=|O\([nN]\)", text)
    stop = {
        "the", "a", "an", "is", "are", "to", "of", "and", "or", "for", "in", "on",
        "it", "that", "this", "but", "not", "they", "their", "because", "so",
        "with", "without", "than", "then", "be", "as", "at", "by", "from", "if",
    }
    kept = [t.lower() for t in tokens if t.lower() not in stop and len(t) >= 2]
    return phrases + kept


def _score_answer(answer_text: str, keywords: list[str]) -> int:
    """Count keyword hits in the answer, weighting quoted phrases x3."""
    if not keywords:
        return 0
    low = answer_text.lower()
    score = 0
    for kw in keywords:
        if " " in kw or len(kw) >= 4:
            # phrase / long token — exact substring match, weighted higher.
            if kw in low:
                score += 3
        elif kw in low:
            score += 1
    return score


def classify_answer(bug: BugEntry, answer_text: str) -> ClassificationResult:
    """Map a probe answer to the best-matching competing hypothesis.

    Returns :class:`ClassificationResult` with ``best_hypothesis`` set to the
    hypothesis key with the highest keyword-overlap score, or None if no
    hypothesis scores above zero. Pure and deterministic; no LLM.

    Keywords come *only* from ``BugEntry.expected_patterns`` — that is the per-
    hypothesis discriminating signal (the shape of answer each hypothesis
    predicts). ``likely_wrong_answers`` is a shared pool of observable errors
    and is NOT positionally mapped to hypotheses; using it would bias the
    classifier toward whichever hypothesis happens to be listed first.
    """
    scores: dict[str, int] = {}
    for hypothesis in bug.competing_hypotheses:
        h_key = hypothesis_key(hypothesis)
        pattern = bug.expected_patterns.get(h_key, "")
        kws = _keywords_from_text(pattern)
        scores[h_key] = _score_answer(answer_text, kws)

    if not any(s > 0 for s in scores.values()):
        return ClassificationResult(best_hypothesis=None, scores=scores,
                                    method="keyword", reason="no hypothesis matched")

    best = max(scores.items(), key=lambda kv: (kv[1], -ord(kv[0][0]) if kv[0] else 0))
    # Tie-break: highest score wins; on a tie the lexicographically-smaller key
    # wins for determinism (max with a stable secondary key).
    top = [k for k, v in scores.items() if v == best[1]]
    winner = min(top) if len(top) > 1 else best[0]
    return ClassificationResult(
        best_hypothesis=winner, scores=scores, method="keyword",
        reason=f"keyword overlap: {winner}={best[1]}",
    )


def classify_with_model(router, bug: BugEntry, answer_text: str) -> ClassificationResult:
    """Live path: ask the model to pick one of the known hypothesis keys.

    Constrained to return a key from ``bug.competing_hypotheses`` or null. On
    any failure or unknown key, falls back to the deterministic
    :func:`classify_answer`.
    """
    keys = [hypothesis_key(h) for h in bug.competing_hypotheses]
    system = (
        "你是诊断分类助手。从给定的假设集合中选出与该学生回答最匹配的一个假设。"
        f"只能返回以下键之一或 null：{keys}。输出严格 JSON："
        '{"hypothesis":"<键或null>","reason":"..."}。'
        "不要编造集合之外的键。"
    )
    user = (
        f"误区描述: {bug.description}\n"
        f"竞争假设: {bug.competing_hypotheses}\n"
        f"各假设预期模式: {bug.expected_patterns}\n"
        f"学生回答: {answer_text}"
    )
    res = router.complete(
        "probe_classify",
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        output_schema={"type": "object"},
        temperature=0.0,
    )
    if not res.ok or res.parsed_json is None:
        return classify_answer(bug, answer_text)
    raw = res.parsed_json.get("hypothesis")
    if raw is None:
        return ClassificationResult(best_hypothesis=None, scores={}, method="llm",
                                    reason="model returned null")
    key = str(raw).strip()
    if key in keys:
        return ClassificationResult(best_hypothesis=key, scores={}, method="llm",
                                    reason=str(res.parsed_json.get("reason", "")))
    # Unknown key — fall back deterministically.
    return classify_answer(bug, answer_text)


def signals_for_probe(bug: BugEntry, answer_text: str, *, router=None) -> "list":
    """Build the FOR MisconceptionSignal for one probe answer.

    A probe that matches a hypothesis writes a single FOR signal for the bug.
    We do NOT write an AGAINST signal for the same bug_id: the competing
    hypotheses in a BugEntry are sub-explanations of *one* observable bug
    (they share one bug_id), so an AGAINST on the same bug_id would be scored
    as an explicit disproof of the very bug the probe supports — contradicting
    the FOR. Mutual exclusion across *different* bug_ids in the same
    hypothesis_group is handled separately by :mod:`mutual_exclusion`.

    When classification fails (None), returns an empty list — the caller
    treats the answer as NEEDS_REVIEW for misconception purposes.
    """
    from ...domain.models import MisconceptionSignal
    from ...domain.enums import SignalDirection, SignalStrength

    if router is not None and _router_live(router):
        result = classify_with_model(router, bug, answer_text)
    else:
        result = classify_answer(bug, answer_text)
    if result.best_hypothesis is None:
        return []
    return [
        MisconceptionSignal(
            bug_id=bug.bug_id,
            direction=SignalDirection.FOR,
            strength=SignalStrength.STRONG,
            reason=f"probe matched {result.best_hypothesis}",
        )
    ]
