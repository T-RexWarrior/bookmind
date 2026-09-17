"""Diagnostician Agent — ARCHITECTURE.md §3.3, LEARNING_MODEL.md §7.

Responsibilities:
  - turn a learner's answer into an :class:`AnswerJudgment`;
  - judge PASS/PARTIAL/FAIL against the rubric (language understanding only);
  - emit misconception signals (bug_id / direction / strength);
  - generate diagnostic probes and two changed tasks of different scenarios.

Constraints (§3.3): never outputs mastery percentages or misconception
probabilities; never decides required_level / hint_level / independent or the
official target_concept_ids (those come from the trusted contexts); may return
NEEDS_REVIEW; outputs only structured judgments — the Engine applies the rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..domain.enums import EvidenceResult, JudgmentStatus, SignalDirection, SignalStrength
from ..domain.models import AnswerJudgment, CriterionResult, MisconceptionSignal, TrustedTaskContext
from ..llm.router import ModelRouter
from ..llm.schemas import ModelResult


DIAGNOSTICIAN_PROMPT_VERSION = "diagnostician_v1"


class DiagnosticianAgent:
    """Judges answers against a rubric and emits misconception signals."""

    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    def judge(
        self,
        task: TrustedTaskContext,
        answer_text: str,
        *,
        rubric: list[str] | None = None,
        prompt_text: str = "",
        expected_answer: str = "",
    ) -> AnswerJudgment:
        """Produce an AnswerJudgment for one answer.

        The model is asked to return JSON matching AnswerJudgment's shape. We
        then coerce and validate it. On any model failure or parse failure we
        fall back to a deterministic offline judge (mirroring the
        probe_classifier / book_mapper offline pattern) so the closed loop is
        exercisable without a live model — the demo and product paths use the
        same service (PRODUCTIZATION §1.3.10). Only when the offline judge is
        itself uncertain do we return NEEDS_REVIEW (LEARNING_MODEL §7), never a
        guessed result.
        """
        rubric = rubric or task.rubric
        res = self._call_model(
            task, answer_text, rubric,
            prompt_text=prompt_text, expected_answer=expected_answer,
        )
        if res.ok and res.parsed_json is not None:
            judgment = _coerce_judgment(res.parsed_json)
            # The live model often invents bug_ids that are not in the Bug Library
            # (it has no way to know the canonical keys unless we tell it — see
            # _call_model, which injects the candidate bugs). Even with that, an
            # unknown bug_id would seed a MisconceptionHypothesis with empty
            # related_concepts that the decision engine can never match, freezing
            # the closure at SUSPECTED. So we normalise signals to the library:
            # drop unknown bug_ids, and if the model judged FAIL/PARTIAL but left
            # no usable signal, fall back to the deterministic concept→bug mapping
            # so a genuinely wrong answer still advances a real misconception
            # (mirroring _offline_judge's quiz path).
            judgment = _normalise_signals(task, judgment, answer_text, rubric)
            # A changed-task PASS is a load-bearing state transition (REMEDIATING
            # → VERIFYING → RESOLVED). The live model is often too strict and
            # returns PARTIAL for a clearly-correct reference/aliasing answer,
            # which would stall the learner in REMEDIATING forever. Reconcile
            # against the deterministic _looks_correct/_looks_wrong cues: a
            # clearly-correct answer is promoted to PASS; a clearly-wrong one is
            # kept at FAIL. The model still wins on genuinely ambiguous answers.
            judgment = _reconcile_changed_task(task, judgment, answer_text, rubric)
            return judgment
        # Model unavailable/unparseable → deterministic offline judge.
        offline = _offline_judge(task, answer_text, rubric, expected_answer=expected_answer)
        if offline is not None:
            return offline
        return AnswerJudgment(judgment_status=JudgmentStatus.NEEDS_REVIEW, result=None,
                              reason=f"model unavailable and offline judge uncertain: {res.error or 'no json'}")

    def _call_model(
        self, task: TrustedTaskContext, answer: str, rubric: list[str], *,
        prompt_text: str = "", expected_answer: str = "",
    ) -> ModelResult:
        targets = ", ".join(task.target_concept_ids)
        levels = ", ".join(l.value for l in task.evidence_for_levels)
        rubric_text = "\n".join(f"- {r}" for r in rubric)
        # Inject the candidate misconceptions for this task so the model returns
        # canonical bug_ids rather than inventing its own. For a probe / changed
        # task the candidates are the task's discriminated bugs; for an ordinary
        # quiz they are the bugs whose related_concepts overlap the task's target
        # concepts. (Same source of truth as _offline_judge / probe_classifier.)
        candidates = _candidate_bugs(task)
        bug_catalog = ""
        if candidates:
            lines = []
            for b in candidates:
                lines.append(f'- bug_id="{b.bug_id}": {b.description}')
            bug_catalog = "\n已知误区（bug_id 必须从下列选取，不得编造）：\n" + "\n".join(lines) + "\n"
        else:
            bug_catalog = "\n本题无已知误区，misconception_signals 留空数组。\n"
        system = (
            "你是判定助手。只依据下面给定的 rubric 判定学生回答，输出严格 JSON。"
            "不要输出掌握度百分比或误区概率。"
            + bug_catalog +
            'JSON 格式: {"judgment_status":"DECIDED|NEEDS_REVIEW",'
            '"result":"PASS|PARTIAL|FAIL|null",'
            '"criterion_results":[{"criterion_id":"...","satisfied":true,"note":"..."}],'
            '"target_concept_results":[{"concept_id":"...","result":"PASS|PARTIAL|FAIL"}],'
            '"misconception_signals":[{"bug_id":"...","direction":"FOR|AGAINST","strength":"WEAK|MEDIUM|STRONG","reason":"..."}],'
            '"reason":"..."}。'
            "misconception_signals 的 bug_id 只能填上面列出的已知 bug_id，否则填空数组。"
            "只有学生没有作答、答案无法辨认，或题目本身确实缺少判分依据时，才设 NEEDS_REVIEW。"
            "学生给出了明确但错误、不完整或与评分点不符的作答时，必须设 DECIDED 且 result=FAIL 或 PARTIAL，不能用 NEEDS_REVIEW 逃避判定。"
        )
        prompt_for_judge = (prompt_text or "未提供题干").strip()[:1400]
        # Older generated tasks may have persisted an entire source excerpt as
        # their expected answer.  It is neither a usable key nor safe to send
        # wholesale to the model: it causes timeouts and hides the actual task.
        expected_for_judge = (expected_answer or "").strip()
        if len(expected_for_judge) > 1200:
            expected_for_judge = "（历史题目的标准答案过长，已忽略；请依据题干和 Rubric 判定。）"
        user = (
            f"题目（仅用于判分，不得在回答中泄露）:\n{prompt_for_judge}\n\n"
            f"服务端标准答案（仅用于判分，不得复述给学生）:\n{expected_for_judge or '未提供标准答案，以 Rubric 为准'}\n\n"
            f"目标概念: {targets}\n验证等级: {levels}\nRubric:\n{rubric_text}\n"
            f"学生回答: {answer}"
        )
        return self.router.complete(
            "diagnostician_judge",
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            output_schema={"type": "object"},
            temperature=0.0,
            max_tokens=512,
        )


def _coerce_judgment(data: dict) -> AnswerJudgment:
    """Validate and coerce a parsed JSON dict into an AnswerJudgment.

    Defensive: bad enum values or missing fields degrade to NEEDS_REVIEW rather
    than raising, so a malformed model output never corrupts state.
    """
    try:
        status_raw = str(data.get("judgment_status", "NEEDS_REVIEW")).upper()
        status = JudgmentStatus(status_raw) if status_raw in ("DECIDED", "NEEDS_REVIEW") else JudgmentStatus.NEEDS_REVIEW
        result = None
        if status == JudgmentStatus.DECIDED:
            rraw = str(data.get("result", "")).upper()
            result = EvidenceResult(rraw) if rraw in ("PASS", "PARTIAL", "FAIL") else None
            if result is None:
                # DECIDED without a valid result → not usable; degrade.
                status = JudgmentStatus.NEEDS_REVIEW
        criterion_results = [_coerce_criterion(c) for c in data.get("criterion_results", []) if isinstance(c, dict)]
        target_results = [_coerce_target(t) for t in data.get("target_concept_results", []) if isinstance(t, dict)]
        signals = [_coerce_signal(s) for s in data.get("misconception_signals", []) if isinstance(s, dict)]
        return AnswerJudgment(
            judgment_status=status, result=result,
            criterion_results=criterion_results, target_concept_results=target_results,
            misconception_signals=signals, reason=str(data.get("reason", "")),
        )
    except Exception as e:  # pragma: no cover - defensive
        return AnswerJudgment(judgment_status=JudgmentStatus.NEEDS_REVIEW, result=None,
                              reason=f"coercion failed: {e}")


def _coerce_criterion(c: dict) -> CriterionResult:
    return CriterionResult(
        criterion_id=str(c.get("criterion_id", "")),
        satisfied=bool(c.get("satisfied", False)),
        note=str(c.get("note", "")),
    )


def _coerce_target(t: dict) -> "TargetConceptResult":  # type: ignore[name-defined]
    from ..domain.models import TargetConceptResult
    rraw = str(t.get("result", "FAIL")).upper()
    return TargetConceptResult(
        concept_id=str(t.get("concept_id", "")),
        result=EvidenceResult(rraw) if rraw in ("PASS", "PARTIAL", "FAIL") else EvidenceResult.FAIL,
    )


def _coerce_signal(s: dict) -> MisconceptionSignal:
    d_raw = str(s.get("direction", "FOR")).upper()
    st_raw = str(s.get("strength", "MEDIUM")).upper()
    return MisconceptionSignal(
        bug_id=str(s.get("bug_id", "")),
        direction=SignalDirection(d_raw) if d_raw in ("FOR", "AGAINST") else SignalDirection.FOR,
        strength=SignalStrength(st_raw) if st_raw in ("WEAK", "MEDIUM", "STRONG") else SignalStrength.MEDIUM,
        reason=str(s.get("reason", "")),
    )


# --- deterministic offline judge ----------------------------------------------
# Mirrors the probe_classifier / book_mapper offline pattern: a pure, explainable
# judge used when no live model is available, so the demo and product paths run
# the *same* Diagnostician service (PRODUCTIZATION §1.3.10). It never sets
# required_level / hint_level / independent — the Engine derives those. When it
# cannot decide confidently it returns None → the caller emits NEEDS_REVIEW, so
# the "never fabricate confidence" safety property (LEARNING_MODEL §7) holds.
#
# Correctness judgment is based on BugEntry.likely_wrong_answers (explicit wrong
# phrasings) + value/explanation cues, NOT on probe_classifier.classify_answer
# alone (that only distinguishes *which* wrong hypothesis, not right vs wrong).

# Cues that indicate a correct understanding of reference aliasing / value copy.
_CORRECT_CUES = ("same object", "same reference", "reference", "aliasing", "alias",
                 "point to the same", "points to the same", "share", "9")
# Cues that indicate the value-copy misconception.
_WRONG_CUES = ("original value", "separate copy", "copies all fields", "copy of",
               "new box", "b is a copy", "a copy", "value is copied")


# Generic sentence-frame words that appear in both correct and wrong answers;
# only *distinctive* content words should count toward a wrong-phrase match.
_WRONG_PHRASE_STOP = {
    "a.getvalue()", "returns", "return", "the", "because", "value", "from", "into",
    "when", "what", "why", "this", "that", "then", "will", "would", "does", "is",
    "are", "was", "were", "they", "their", "it", "a", "an", "of", "to", "and", "or",
    "for", "in", "on", "with", "without", "not", "be", "as", "at", "by", "if",
}


def _answer_hits_wrong_phrase(bug, answer: str) -> bool:
    """True if the answer closely matches one of the bug's likely_wrong_answers.

    Matches on the bug's *distinctive* content words (excluding generic sentence
    frame like "returns/because/value"), so a correct answer that shares only the
    question's wording is not misjudged as wrong.
    """
    low = answer.lower()
    for wa in bug.likely_wrong_answers:
        phrase = wa.lower()
        if len(phrase) >= 12 and phrase in low:
            return True
        # Chinese diagnostic examples are often written as one uninterrupted
        # phrase, and learners normally omit its final punctuation. Compare a
        # compact form as well, without turning short generic words into cues.
        compact_phrase = "".join(ch for ch in phrase if ch.isalnum())
        compact_answer = "".join(ch for ch in low if ch.isalnum())
        if len(compact_phrase) >= 4 and compact_phrase in compact_answer:
            return True
        distinctive = [w.strip(".,;:!?") for w in phrase.split()
                       if len(w) >= 4 and w.strip(".,;:!?") not in _WRONG_PHRASE_STOP]
        if distinctive and sum(1 for w in distinctive if w in low) >= 2:
            return True
    return False


def _looks_wrong(bug, answer: str) -> bool:
    low = answer.lower()
    if _answer_hits_wrong_phrase(bug, answer):
        return True
    # Value-copy cue present and no correct cue → wrong.
    if any(c in low for c in _WRONG_CUES) and not any(c in low for c in _CORRECT_CUES):
        return True
    return False


def _looks_correct(bug, answer: str) -> bool:
    low = answer.lower()
    if _answer_hits_wrong_phrase(bug, answer):
        return False
    # A correct answer names the same-object/reference relationship and the right
    # outcome, without echoing a likely wrong phrase.
    has_correct = any(c in low for c in _CORRECT_CUES)
    has_wrong = any(c in low for c in _WRONG_CUES)
    return has_correct and not has_wrong


def _bugs_for_concept(concept_id: str):
    """Return BugEntries whose related_concepts include this concept."""
    from .bug_library import BUG_LIBRARY
    return [b for b in BUG_LIBRARY.values() if concept_id in b.related_concepts]


def _candidate_bugs(task: TrustedTaskContext):
    """The misconceptions the model may legitimately cite for this task.

    For a probe / changed task: the task's own discriminated bugs. For an
    ordinary quiz: every bug whose related_concepts overlap the task's target
    concepts (same mapping _offline_judge uses). De-duplicated, order-stable.
    """
    from .bug_library import BUG_LIBRARY
    seen: set[str] = set()
    out: list = []
    for bid in task.discriminated_bug_ids or []:
        if bid in BUG_LIBRARY and bid not in seen:
            seen.add(bid)
            out.append(BUG_LIBRARY[bid])
    for cid in task.target_concept_ids or []:
        for b in _bugs_for_concept(cid):
            if b.bug_id not in seen:
                seen.add(b.bug_id)
                out.append(b)
    return out


def _normalise_signals(
    task: TrustedTaskContext, judgment: AnswerJudgment, answer_text: str, rubric: list[str],
) -> AnswerJudgment:
    """Constrain model-emitted misconception signals to the Bug Library.

    The live model sometimes invents bug_ids (no prompt can fully prevent it).
    An unknown bug_id is dropped — it would seed a MisconceptionHypothesis with
    empty related_concepts that the decision engine can never match, freezing
    the closure at SUSPECTED/LIKELY forever.

    If after dropping we have no usable signal but the model judged the answer
    wrong (FAIL/PARTIAL), fall back to the deterministic concept→bug mapping so
    a genuinely wrong quiz answer still advances a real misconception. This is
    the same _bugs_for_concept + _looks_wrong logic as _offline_judge's quiz
    path, used only to recover a valid bug_id the model failed to name. For a
    probe / changed task we never synthesise signals — the task's own
    discriminated_bug_ids + the probe_classifier handle those (and
    _attach_probe_signals re-classifies when the model emits none).
    """
    from .bug_library import BUG_LIBRARY
    valid = [s for s in judgment.misconception_signals if s.bug_id in BUG_LIBRARY]
    if valid:
        if valid != judgment.misconception_signals:
            return judgment.model_copy(update={"misconception_signals": valid})
        return judgment

    # No valid signal. Only attempt a deterministic recovery for an ordinary
    # wrong quiz answer — never for probes/changed tasks (those are classified
    # elsewhere) and never for PASS (nothing to support).
    if task.is_probe or task.is_changed_task:
        return judgment.model_copy(update={"misconception_signals": []})
    if judgment.result not in (EvidenceResult.FAIL, EvidenceResult.PARTIAL):
        return judgment.model_copy(update={"misconception_signals": []})

    answer = (answer_text or "").strip()
    if not answer:
        return judgment.model_copy(update={"misconception_signals": []})
    candidates = []
    seen_bug_ids = set()
    # Imported textbooks have their own concept ids.  A task may therefore
    # carry a semantically matched BugEntry even though its target id is not a
    # legacy demo id such as c_queue.
    for bid in task.discriminated_bug_ids or []:
        bug = BUG_LIBRARY.get(bid)
        if bug is not None and bid not in seen_bug_ids:
            candidates.append(bug)
            seen_bug_ids.add(bid)
    for cid in task.target_concept_ids or []:
        for bug in _bugs_for_concept(cid):
            if bug.bug_id not in seen_bug_ids:
                candidates.append(bug)
                seen_bug_ids.add(bug.bug_id)
    for bug in candidates:
        if _looks_wrong(bug, answer):
            sig = _signal(bug.bug_id, SignalDirection.FOR, SignalStrength.WEAK,
                          "diagnostician: model signal remapped to known bug")
            return judgment.model_copy(update={"misconception_signals": [sig]})
    return judgment.model_copy(update={"misconception_signals": []})


def _reconcile_changed_task(
    task: TrustedTaskContext, judgment: AnswerJudgment, answer_text: str, rubric: list[str],
) -> AnswerJudgment:
    """Reconcile a live-model changed-task judgment with the deterministic cues.

    The changed-task PASS is load-bearing: REMEDIATING → VERIFYING → RESOLVED
    depends on it. Live models are often too strict and return PARTIAL for a
    clearly-correct reference/aliasing answer, which stalls the learner in
    REMEDIATING indefinitely. For a changed task we cross-check the answer
    against the bug's known correct/wrong cues (the same _looks_correct /
    _looks_wrong the offline judge trusts) and override only when the cues are
    unambiguous:

      - clearly correct (correct cues, no wrong cues) → promote to PASS
      - clearly wrong   (wrong-answer pattern / wrong cues) → keep/force FAIL

    On anything ambiguous the model's judgment stands. This is conservative:
    it only changes a result when the deterministic cues are decisive, and it
    never raises a FAIL/PARTIAL to PASS on a merely "not wrong" answer.
    """
    if not task.is_changed_task or not task.discriminated_bug_ids:
        return judgment
    if judgment.judgment_status != JudgmentStatus.DECIDED:
        return judgment
    from .bug_library import BUG_LIBRARY
    bug = BUG_LIBRARY.get(task.discriminated_bug_ids[0])
    if bug is None:
        return judgment
    answer = (answer_text or "").strip()
    if not answer:
        return judgment

    clearly_correct = _looks_correct(bug, answer)
    clearly_wrong = _looks_wrong(bug, answer)
    if clearly_correct and not clearly_wrong and judgment.result != EvidenceResult.PASS:
        return _decided(EvidenceResult.PASS, "changed task: deterministic cues confirm correct understanding",
                        rubric, judgment.misconception_signals)
    if clearly_wrong and judgment.result == EvidenceResult.PASS:
        # The model passed an answer that matches a known wrong pattern — trust
        # the cues and force FAIL so a wrong transfer does not fake RESOLVED.
        sig = _signal(bug.bug_id, SignalDirection.FOR, SignalStrength.MEDIUM,
                      "changed task: deterministic cues flag a wrong-answer pattern")
        return _decided(EvidenceResult.FAIL, "changed task: matched wrong-answer pattern",
                        rubric, [sig])
    return judgment


def _signal(bug_id, direction, strength, reason) -> MisconceptionSignal:
    return MisconceptionSignal(bug_id=bug_id, direction=direction, strength=strength, reason=reason)


def _criteria(rubric, satisfied: bool) -> list[CriterionResult]:
    return [CriterionResult(criterion_id=str(i), satisfied=satisfied, note="")
            for i in range(len(rubric))]


def _decided(result, reason, rubric, signals=None) -> AnswerJudgment:
    return AnswerJudgment(
        judgment_status=JudgmentStatus.DECIDED, result=result,
        criterion_results=_criteria(rubric, result == EvidenceResult.PASS),
        misconception_signals=signals or [], reason=reason,
    )


def _offline_judge(
    task: TrustedTaskContext, answer_text: str, rubric: list[str], *, expected_answer: str = "",
) -> AnswerJudgment | None:
    """Deterministic offline judge. Returns None when uncertain (→ NEEDS_REVIEW)."""
    from .bug_library import BUG_LIBRARY
    from ..engine.misconception.probe_classifier import classify_answer

    answer = (answer_text or "").strip()
    rubric = rubric or []

    # --- diagnostic probe: discriminate one bug's hypotheses ----------------
    if task.is_probe and task.discriminated_bug_ids:
        bug = BUG_LIBRARY.get(task.discriminated_bug_ids[0])
        if bug is None:
            return None
        if not answer:
            return None
        if _looks_wrong(bug, answer):
            cls = classify_answer(bug, answer)
            hyp = cls.best_hypothesis
            reason = (f"offline: probe matched {hyp}" if hyp
                      else "offline: matched wrong-answer pattern")
            return _decided(EvidenceResult.FAIL, reason, rubric,
                            [_signal(bug.bug_id, SignalDirection.FOR, SignalStrength.STRONG, reason)])
        if _looks_correct(bug, answer):
            return _decided(EvidenceResult.PASS, "offline: correct reference/aliasing explanation",
                            rubric)
        # Cannot confidently tell → let the caller emit NEEDS_REVIEW.
        return None

    # --- changed task (remediation verification) ----------------------------
    if task.is_changed_task and task.discriminated_bug_ids:
        bug = BUG_LIBRARY.get(task.discriminated_bug_ids[0])
        if bug is None or not answer:
            return None
        if _looks_wrong(bug, answer):
            reason = "offline: changed task matched wrong-answer pattern"
            return _decided(EvidenceResult.FAIL, reason, rubric,
                            [_signal(bug.bug_id, SignalDirection.FOR, SignalStrength.MEDIUM, reason)])
        # A changed task passes only when the answer shows the *correct*
        # understanding (the right cues, no wrong cues) — not merely when it is
        # long. An unrelated long sentence must not count as a transfer pass.
        if _looks_correct(bug, answer):
            return _decided(EvidenceResult.PASS, "offline: changed task answer shows correct understanding",
                            rubric)
        return None

    # --- ordinary quiz ------------------------------------------------------
    # The offline judge can only *confidently* identify a wrong answer when it
    # matches one of the bug's known wrong-answer patterns. It has no reliable
    # way to confirm a free-text answer is *correct* (a long unrelated sentence
    # is not a correct explanation). LEARNING_MODEL §7 "never fabricate
    # confidence": an answer that matches no wrong pattern → NEEDS_REVIEW so the
    # Engine does not raise mastery on a guess. Only a live model can PASS a
    # free-text answer; the offline path never does (PRODUCTIZATION §1.3.10).
    concept_id = task.target_concept_ids[0] if task.target_concept_ids else ""
    if concept_id:
        for bug in _bugs_for_concept(concept_id):
            if _looks_wrong(bug, answer):
                reason = "offline: quiz matched likely wrong answer"
                return _decided(EvidenceResult.FAIL, reason, rubric,
                                [_signal(bug.bug_id, SignalDirection.FOR, SignalStrength.WEAK, reason)])
    # When the online judge times out, do not throw away an objectively visible
    # part of a numeric/formula answer. This can only produce PARTIAL (never
    # PASS), so it cannot falsely promote mastery. It is intentionally narrow:
    # a formula such as O(n) or an exact numeric result must occur in both the
    # server-only answer key and the learner response.
    shared = _shared_checkable_tokens(answer, expected_answer)
    if shared:
        criteria = [CriterionResult(
            criterion_id=str(index),
            satisfied=any(token.casefold() in criterion.casefold() for token in shared),
            note="",
        ) for index, criterion in enumerate(rubric)]
        return AnswerJudgment(
            judgment_status=JudgmentStatus.DECIDED,
            result=EvidenceResult.PARTIAL,
            criterion_results=criteria,
            reason="offline: matched checkable answer-key token after model timeout",
        )
    # No confident wrong-answer match and no live model → let the caller emit
    # NEEDS_REVIEW. Do NOT fall back to a length-based PASS.
    return None


def _shared_checkable_tokens(answer: str, expected_answer: str) -> list[str]:
    """Find unambiguous formula/numeric tokens shared with an answer key."""
    if not expected_answer:
        return []
    pattern = re.compile(r"(?:[OΘΩ]\s*\([^)]{1,24}\)|\b\d+(?:\.\d+)?\b|\b[A-Za-z_]\w*\s*=\s*\d+\b)")
    expected = {re.sub(r"\s+", "", token) for token in pattern.findall(expected_answer)}
    learner = re.sub(r"\s+", "", answer)
    return sorted(token for token in expected if len(token) >= 3 and token in learner)
