"""Diagnostician human gold answer set — EVALUATION.md §3.1/§3.3.

A hand-annotated fixture of learner answers covering the 8 required answer
categories per core bug:

    typical_correct, typical_wrong, partial, colloquial,
    code_nl, ambiguous, off_topic, hint_trace

Each case fixes a TrustedTaskContext, an InteractionContext, the answer text,
the gold AnswerJudgment, and (for the classification test) the hypothesis the
probe classifier should match. This is the L2 component-quality dataset: it
prevents "structured output is valid but semantically wrong".

The classification test runs the *deterministic* probe classifier on the
typical_wrong / colloquial / code_nl cases and asserts it hits the gold
hypothesis — proving "在固定人工回答集上通过分类测试" (LEARNING_MODEL §9).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import (
    ActivityMode, EvidenceResult, EvidenceType, HintLevel, InterventionPolicy,
    JudgmentStatus, Level, SignalDirection, SignalStrength, UIPreset,
)
from ..domain.models import (
    AnswerJudgment, InteractionContext, MisconceptionSignal, TrustedTaskContext,
)
from ..agents.bug_library import BUG_LIBRARY


# The 8 answer categories (EVALUATION §3.1).
CATEGORY_TYPICAL_CORRECT = "typical_correct"
CATEGORY_TYPICAL_WRONG = "typical_wrong"
CATEGORY_PARTIAL = "partial"
CATEGORY_COLLOQUIAL = "colloquial"
CATEGORY_CODE_NL = "code_nl"
CATEGORY_AMBIGUOUS = "ambiguous"
CATEGORY_OFF_TOPIC = "off_topic"
CATEGORY_HINT_TRACE = "hint_trace"

ALL_CATEGORIES = [
    CATEGORY_TYPICAL_CORRECT, CATEGORY_TYPICAL_WRONG, CATEGORY_PARTIAL,
    CATEGORY_COLLOQUIAL, CATEGORY_CODE_NL, CATEGORY_AMBIGUOUS,
    CATEGORY_OFF_TOPIC, CATEGORY_HINT_TRACE,
]


@dataclass
class DiagnosticianGoldCase:
    case_id: str
    bug_id: str
    category: str
    answer_text: str
    task: TrustedTaskContext
    interaction: InteractionContext
    gold_judgment: AnswerJudgment
    gold_hypothesis: str | None = None  # for the classification test (probe answers)


def _probe_task(bug_id: str) -> TrustedTaskContext:
    bug = BUG_LIBRARY[bug_id]
    return TrustedTaskContext(
        task_id=f"probe|{bug_id}", task_version=1,
        target_concept_ids=list(bug.related_concepts),
        evidence_for_levels=[Level.L2], rubric=list(bug.rubric),
        is_probe=True, discriminated_bug_ids=[bug_id],
    )


def _interaction(hints: int = 0) -> InteractionContext:
    return InteractionContext(
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING, hints_issued=hints,
    )


def _judgment(result, bug_id=None, direction=None, strength=SignalStrength.MEDIUM, reason=""):
    signals = []
    if bug_id and direction:
        signals.append(MisconceptionSignal(bug_id=bug_id, direction=direction, strength=strength))
    return AnswerJudgment(
        judgment_status=JudgmentStatus.DECIDED, result=result,
        misconception_signals=signals, reason=reason,
    )


# --------------------------------------------------------------------------
# Build the gold set: 8 categories × the first bug (bug_ref_vs_object), plus
# typical_wrong + colloquial + code_nl cases for the other four bugs (the
# categories the classification test exercises).
# --------------------------------------------------------------------------

def _build_gold() -> list[DiagnosticianGoldCase]:
    cases: list[DiagnosticianGoldCase] = []
    bug_id = "bug_ref_vs_object"
    bid = bug_id

    # --- bug_ref_vs_object: all 8 categories ---
    cases.append(DiagnosticianGoldCase(
        "gc_b1_correct", bid, CATEGORY_TYPICAL_CORRECT,
        "a.getValue() returns 9, because b and a refer to the same object; assigning b = a copies the reference, not the object.",
        _probe_task(bid), _interaction(),
        _judgment(EvidenceResult.PASS, reason="correctly identifies aliasing"),
    ))
    cases.append(DiagnosticianGoldCase(
        "gc_b1_wrong", bid, CATEGORY_TYPICAL_WRONG,
        "a.getValue() returns the original value because b is a separate copy.",
        _probe_task(bid), _interaction(),
        _judgment(EvidenceResult.FAIL, bid, SignalDirection.FOR, SignalStrength.STRONG,
                  reason="value-semantics misconception"),
        gold_hypothesis="h_value_semantics",
    ))
    cases.append(DiagnosticianGoldCase(
        "gc_b1_partial", bid, CATEGORY_PARTIAL,
        "a and b are connected somehow, so maybe 9? I'm not sure why.",
        _probe_task(bid), _interaction(),
        _judgment(EvidenceResult.PARTIAL, reason="partial understanding of aliasing"),
    ))
    cases.append(DiagnosticianGoldCase(
        "gc_b1_colloquial", bid, CATEGORY_COLLOQUIAL,
        "a and b share the same object, so a.getValue() is 9 — they're connected.",
        _probe_task(bid), _interaction(),
        _judgment(EvidenceResult.PASS, reason="colloquial but correct aliasing intuition"),
        gold_hypothesis="h_ref_aliasing",
    ))
    cases.append(DiagnosticianGoldCase(
        "gc_b1_code_nl", bid, CATEGORY_CODE_NL,
        "Box b = a; b.setValue(9); // 这里 a.getValue() 返回 1，因为 b 是 a 的副本。",
        _probe_task(bid), _interaction(),
        _judgment(EvidenceResult.FAIL, bid, SignalDirection.FOR, SignalStrength.STRONG,
                  reason="code + NL showing value-semantics misconception"),
        gold_hypothesis="h_value_semantics",
    ))
    cases.append(DiagnosticianGoldCase(
        "gc_b1_ambiguous", bid, CATEGORY_AMBIGUOUS,
        "Maybe 9, maybe 1, it depends on how Java works.",
        _probe_task(bid), _interaction(),
        _judgment(EvidenceResult.PARTIAL, reason="ambiguous; cannot decide"),
    ))
    cases.append(DiagnosticianGoldCase(
        "gc_b1_off_topic", bid, CATEGORY_OFF_TOPIC,
        "I think loops are hard.",
        _probe_task(bid), _interaction(),
        _judgment(EvidenceResult.FAIL, reason="off-topic; no relevant content"),
    ))
    cases.append(DiagnosticianGoldCase(
        "gc_b1_hint_trace", bid, CATEGORY_HINT_TRACE,
        "The hint said they share memory, so a.getValue() is 9.",
        _probe_task(bid), _interaction(hints=2),
        _judgment(EvidenceResult.PASS, reason="correct but hint-assisted; not independent"),
    ))

    # --- the other four bugs: classification-test cases (wrong/colloquial/code_nl) ---
    _add_other_bug_cases(cases, "bug_eq_vs_equals",
        wrong="== compares the contents of two Strings, so it returns true.",
        wrong_hyp="h_eq_is_content",
        colloquial="两个 String 内容一样，== 肯定是 true 啊。",
        colloquial_hyp="h_eq_is_content",
        code='new String("hi") == new String("hi") // 返回 true，因为比较的是内容',
        code_hyp="h_eq_is_content",
    )
    _add_other_bug_cases(cases, "bug_equals_no_hashcode",
        wrong="One element remains in the HashSet because equals handles dedup; the default hashCode is irrelevant.",
        wrong_hyp="h_hashcode_unused",
        colloquial="只要 equals 相等，HashSet 就剩一个元素，靠 equals handles dedup 去重。",
        colloquial_hyp="h_hashcode_unused",
        code="Override equals only → HashSet keeps one element. // equals handles dedup",
        code_hyp="h_hashcode_unused",
    )
    _add_other_bug_cases(cases, "bug_static_dispatch",
        wrong="Animal.speak() runs because the declared type is Animal.",
        wrong_hyp="h_static_binding",
        colloquial="声明的类型是 Animal，所以调用 Animal 的 speak()。",
        colloquial_hyp="h_static_binding",
        code="Animal a = new Dog(); a.speak(); // 调用 Animal.speak()",
        code_hyp="h_static_binding",
    )
    _add_other_bug_cases(cases, "bug_collection_choice",
        wrong="HashSet preserves insertion order.",
        wrong_hyp="h_set_unordered",
        colloquial="要保留插入顺序就用 HashSet 就行了。",
        colloquial_hyp="h_set_unordered",
        code="Set<String> s = new HashSet<>(); // 保留插入顺序",
        code_hyp="h_set_unordered",
    )
    return cases


def _add_other_bug_cases(cases, bug_id, *, wrong, wrong_hyp, colloquial, colloquial_hyp, code, code_hyp):
    bid = bug_id
    cases.append(DiagnosticianGoldCase(
        f"gc_{bid}_wrong", bid, CATEGORY_TYPICAL_WRONG, wrong,
        _probe_task(bid), _interaction(),
        _judgment(EvidenceResult.FAIL, bid, SignalDirection.FOR, SignalStrength.STRONG),
        gold_hypothesis=wrong_hyp,
    ))
    cases.append(DiagnosticianGoldCase(
        f"gc_{bid}_colloquial", bid, CATEGORY_COLLOQUIAL, colloquial,
        _probe_task(bid), _interaction(),
        _judgment(EvidenceResult.FAIL, bid, SignalDirection.FOR, SignalStrength.STRONG),
        gold_hypothesis=colloquial_hyp,
    ))
    cases.append(DiagnosticianGoldCase(
        f"gc_{bid}_code_nl", bid, CATEGORY_CODE_NL, code,
        _probe_task(bid), _interaction(),
        _judgment(EvidenceResult.FAIL, bid, SignalDirection.FOR, SignalStrength.STRONG),
        gold_hypothesis=code_hyp,
    ))


DIAGNOSTICIAN_GOLD: list[DiagnosticianGoldCase] = _build_gold()


# --------------------------------------------------------------------------
# Classification test runner (EVALUATION §3.3, LEARNING_MODEL §9)
# --------------------------------------------------------------------------

@dataclass
class ClassificationTestResult:
    case_id: str
    bug_id: str
    category: str
    expected: str | None
    got: str | None
    passed: bool


def run_probe_classification() -> list[ClassificationTestResult]:
    """Run the deterministic probe classifier on every gold case that has a
    ``gold_hypothesis`` and check it matches.

    This is the "在固定人工回答集上通过分类测试" gate. Pure; no LLM.
    """
    from ..engine.misconception.probe_classifier import classify_answer

    results: list[ClassificationTestResult] = []
    for gc in DIAGNOSTICIAN_GOLD:
        if gc.gold_hypothesis is None:
            continue
        bug = BUG_LIBRARY[gc.bug_id]
        res = classify_answer(bug, gc.answer_text)
        ok = res.best_hypothesis == gc.gold_hypothesis
        results.append(ClassificationTestResult(
            case_id=gc.case_id, bug_id=gc.bug_id, category=gc.category,
            expected=gc.gold_hypothesis, got=res.best_hypothesis, passed=ok,
        ))
    return results


def run_diagnostician_gold(agent) -> list[tuple[str, bool, str]]:
    """Run a DiagnosticianAgent against the gold set and compare judgments.

    Returns ``(case_id, passed, detail)`` tuples. ``agent`` must be a
    DiagnosticianAgent (live or fake router). Comparison checks the result and
    the FOR/AGAINST direction of the first misconception signal — not the
    reason text (EVALUATION: "不要求生成文本逐字一致").
    """
    out: list[tuple[str, bool, str]] = []
    for gc in DIAGNOSTICIAN_GOLD:
        j = agent.judge(gc.task, gc.answer_text, rubric=gc.task.rubric)
        ok = j.judgment_status == gc.gold_judgment.judgment_status
        detail = ""
        if gc.gold_judgment.result is not None:
            if j.result != gc.gold_judgment.result:
                ok = False
                detail = f"result {j.result} != gold {gc.gold_judgment.result}"
        if gc.gold_judgment.misconception_signals and j.misconception_signals:
            g_dir = gc.gold_judgment.misconception_signals[0].direction
            j_dir = j.misconception_signals[0].direction
            if g_dir != j_dir:
                ok = False
                detail = f"signal dir {j_dir} != gold {g_dir}"
        out.append((gc.case_id, ok, detail))
    return out
