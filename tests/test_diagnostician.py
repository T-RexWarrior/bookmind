"""L1/L2 tests: Diagnostician Agent — ARCHITECTURE.md §3.3, LEARNING_MODEL.md §7.

The Diagnostician never decides mastery or hints; on any model/parse failure it
returns NEEDS_REVIEW (no state increment). These tests use a fake router so no
network is needed.
"""

from __future__ import annotations

from dataclasses import dataclass

from bookmind.agents.diagnostician import DiagnosticianAgent, _coerce_judgment
from bookmind.domain.enums import EvidenceResult, JudgmentStatus, Level, SignalDirection, SignalStrength
from bookmind.domain.models import AnswerJudgment, TrustedTaskContext
from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.llm.schemas import ModelResult


class FakeRouter(ModelRouter):
    def __init__(self, parsed=None, fail=False):
        super().__init__(RouterConfig(live=False))
        self._parsed = parsed
        self._fail = fail

    def complete(self, task, messages, *, output_schema=None, temperature=None, max_tokens=None):
        if self._fail:
            return ModelResult(ok=False, task=task, model="fake", content=None, parsed_json=None,
                               error="down", fallback=True)
        return ModelResult(ok=True, task=task, model="fake", content="{}", parsed_json=self._parsed)


def _task():
    return TrustedTaskContext(
        task_id="t1", task_version=1, target_concept_ids=["c_equals"],
        evidence_for_levels=[Level.L2], rubric=["mentions content comparison", "no reference mixup"],
    )


def test_judge_decided_pass():
    parsed = {
        "judgment_status": "DECIDED", "result": "PASS",
        "criterion_results": [{"criterion_id": "c1", "satisfied": True, "note": ""}],
        "target_concept_results": [{"concept_id": "c_value_equality", "result": "PASS"}],
        "misconception_signals": [{"bug_id": "bug_eq_vs_equals", "direction": "AGAINST", "strength": "STRONG"}],
        "reason": "content comparison correct",
    }
    d = DiagnosticianAgent(FakeRouter(parsed=parsed))
    j = d.judge(_task(), "equals 比较内容")
    assert j.judgment_status == JudgmentStatus.DECIDED
    assert j.result == EvidenceResult.PASS
    assert j.misconception_signals[0].direction == SignalDirection.AGAINST
    assert j.misconception_signals[0].strength == SignalStrength.STRONG


def test_judge_model_failure_falls_back_to_offline_judge():
    """When the live model fails, the Diagnostician falls back to the
    deterministic offline judge (PRODUCTIZATION §1.3.10: demo and product use
    the same service). A *wrong* answer that matches a known wrong pattern is
    judged DECIDED FAIL; a free-text answer the offline judge cannot confidently
    confirm correct returns NEEDS_REVIEW (LEARNING_MODEL §7: never fabricate
    confidence — a long unrelated answer is not a PASS)."""
    d = DiagnosticianAgent(FakeRouter(fail=True))
    # A wrong answer matching bug_ref_vs_object's wrong cues → FAIL.
    task = TrustedTaskContext(
        task_id="t1", task_version=1, target_concept_ids=["c_reference"],
        evidence_for_levels=[Level.L2], rubric=["mentions shared reference"],
    )
    j = d.judge(task, "a and b are separate copies with the original value")
    assert j.judgment_status == JudgmentStatus.DECIDED
    assert j.result == EvidenceResult.FAIL


def test_judge_model_failure_uncertain_returns_needs_review():
    """The offline judge preserves the safety property: when it is genuinely
    uncertain (empty / trivial answer) it returns NEEDS_REVIEW, never a guess."""
    d = DiagnosticianAgent(FakeRouter(fail=True))
    j = d.judge(_task(), "")
    assert j.judgment_status == JudgmentStatus.NEEDS_REVIEW
    assert j.result is None


def test_judge_model_timeout_can_record_safe_partial_from_answer_key():
    d = DiagnosticianAgent(FakeRouter(fail=True))
    j = d.judge(
        _task(), "时间复杂度为 O(n)",
        expected_answer="时间复杂度为 O(n)，并需要处理空数组。",
    )
    assert j.judgment_status == JudgmentStatus.DECIDED
    assert j.result == EvidenceResult.PARTIAL


def test_judge_decided_without_valid_result_degrades_to_needs_review():
    parsed = {"judgment_status": "DECIDED", "result": "BANANA", "misconception_signals": []}
    d = DiagnosticianAgent(FakeRouter(parsed=parsed))
    j = d.judge(_task(), "x")
    assert j.judgment_status == JudgmentStatus.NEEDS_REVIEW
    assert j.result is None


def test_judge_needs_review_with_result_is_rejected_by_schema():
    # The AnswerJudgment model itself rejects NEEDS_REVIEW + a result.
    import pytest
    with pytest.raises(Exception):
        AnswerJudgment(judgment_status=JudgmentStatus.NEEDS_REVIEW, result=EvidenceResult.PASS)


def test_coerce_bad_enum_values_default_safely():
    j = _coerce_judgment({
        "judgment_status": "DECIDED", "result": "PASS",
        "misconception_signals": [{"bug_id": "b1", "direction": "SIDEWAYS", "strength": "MEGA"}],
    })
    assert j.misconception_signals[0].direction == SignalDirection.FOR  # defaulted
    assert j.misconception_signals[0].strength == SignalStrength.MEDIUM  # defaulted


def test_coerce_garbage_dict_returns_needs_review():
    j = _coerce_judgment({"judgment_status": "WAT", "result": "NOPE"})
    assert j.judgment_status == JudgmentStatus.NEEDS_REVIEW
    assert j.result is None


# --- live-path bug_id normalisation (P0 fix) -------------------------------
# The live model can return bug_ids that are not in the Bug Library. Such a
# signal must not seed a MisconceptionHypothesis with empty related_concepts
# (that freezes the closure at SUSPECTED forever). See
# bookmind-live-path-broken-2026-09-05.md.

class _CapturingRouter(ModelRouter):
    """FakeRouter that records the prompt and returns a preset parsed JSON."""

    def __init__(self, parsed):
        super().__init__(RouterConfig(live=True))
        self._parsed = parsed
        self.captured_messages = None

    def complete(self, task, messages, *, output_schema=None, temperature=None, max_tokens=None):
        self.captured_messages = messages
        return ModelResult(ok=True, task=task, model="fake", content="{}", parsed_json=self._parsed)


def _ref_task(is_probe=False, discriminated=None):
    return TrustedTaskContext(
        task_id="t1", task_version=1, target_concept_ids=["c_reference"],
        evidence_for_levels=[Level.L2], rubric=["mentions shared reference"],
        is_probe=is_probe, is_changed_task=False,
        discriminated_bug_ids=discriminated or [],
    )


def test_live_prompt_injects_known_bug_ids():
    """The system prompt must list the candidate bug_ids so the model does not
    invent its own."""
    router = _CapturingRouter(parsed={"judgment_status": "DECIDED", "result": "PASS"})
    DiagnosticianAgent(router).judge(_ref_task(), "a and b share the same reference")
    system = router.captured_messages[0]["content"]
    assert "bug_ref_vs_object" in system
    assert "不得编造" in system or "只能从下列选取" in system or "必须从下列选取" in system


def test_live_judge_receives_server_only_question_and_expected_answer():
    router = _CapturingRouter(parsed={"judgment_status": "DECIDED", "result": "FAIL"})
    DiagnosticianAgent(router).judge(
        _ref_task(), "1000", prompt_text="n=4096 时预计运行时间是多少？",
        expected_answer="应先推导复杂度，再按增长率计算。",
    )
    user = router.captured_messages[1]["content"]
    assert "n=4096" in user
    assert "按增长率计算" in user


def test_live_judge_does_not_send_oversized_legacy_answer_key():
    router = _CapturingRouter(parsed={"judgment_status": "DECIDED", "result": "FAIL"})
    DiagnosticianAgent(router).judge(
        _ref_task(), "1000", prompt_text="请计算运行时间", expected_answer="x" * 1300,
    )
    user = router.captured_messages[1]["content"]
    assert "x" * 200 not in user
    assert "标准答案过长" in user


def test_live_invented_bug_id_is_dropped_on_pass():
    """A PASS with an invented bug_id (AGAINST the bug) drops the signal: there
    is no real misconception to record, and PASS never recovers a signal."""
    parsed = {
        "judgment_status": "DECIDED", "result": "PASS",
        "misconception_signals": [{"bug_id": "primitive_copy_semantics",
                                   "direction": "AGAINST", "strength": "STRONG"}],
    }
    j = DiagnosticianAgent(_CapturingRouter(parsed=parsed)).judge(_ref_task(), "correct answer")
    assert j.result == EvidenceResult.PASS
    assert j.misconception_signals == []


def test_live_invented_bug_id_on_fail_recovers_known_bug():
    """A FAIL whose invented bug_id is dropped is recovered by mapping the wrong
    answer to a known bug (deterministic _looks_wrong), so a real misconception
    advances instead of being lost."""
    parsed = {
        "judgment_status": "DECIDED", "result": "FAIL",
        "misconception_signals": [{"bug_id": "primitive_vs_object_copy",
                                   "direction": "FOR", "strength": "MEDIUM"}],
    }
    j = DiagnosticianAgent(_CapturingRouter(parsed=parsed)).judge(
        _ref_task(), "a.getValue() returns the original value because b is a separate copy.")
    assert j.result == EvidenceResult.FAIL
    assert len(j.misconception_signals) == 1
    assert j.misconception_signals[0].bug_id == "bug_ref_vs_object"



def test_imported_queue_task_recovers_fifo_lifo_bug_from_task_catalogue():
    """Imported books use arbitrary ids, so the task catalogue is the bridge.

    A clear FIFO/LIFO error must become a diagnosable fact even when the
    model emits an invented label and the target is not a legacy demo id.
    """
    parsed = {
        "judgment_status": "DECIDED", "result": "FAIL",
        "misconception_signals": [{"bug_id": "made_up_queue_bug", "direction": "FOR", "strength": "MEDIUM"}],
    }
    task = TrustedTaskContext(
        task_id="queue-real-book", task_version=1, target_concept_ids=["sec_4_5_queue"],
        evidence_for_levels=[Level.L1], rubric=["FIFO", "enqueue rear", "dequeue front"],
        discriminated_bug_ids=["bug_queue_fifo_lifo"],
    )
    j = DiagnosticianAgent(_CapturingRouter(parsed=parsed)).judge(task, "从队尾出队")
    assert j.result == EvidenceResult.FAIL
    assert [item.bug_id for item in j.misconception_signals] == ["bug_queue_fifo_lifo"]


def test_live_probe_does_not_synthesise_signals():
    """A probe never gets synthesised quiz-style signals: its own
    discriminated_bug_ids + the probe_classifier handle classification."""
    parsed = {
        "judgment_status": "DECIDED", "result": "FAIL",
        "misconception_signals": [{"bug_id": "made_up_probe_bug",
                                   "direction": "FOR", "strength": "STRONG"}],
    }
    task = _ref_task(is_probe=True, discriminated=["bug_ref_vs_object"])
    j = DiagnosticianAgent(_CapturingRouter(parsed=parsed)).judge(task, "some wrong probe answer")
    # Invented signal dropped; no synthesis for probes → empty (the Engine's
    # _attach_probe_signals / signals_for_probe re-classifies downstream).
    assert j.misconception_signals == []


def test_live_valid_bug_id_signal_is_preserved():
    """A real bug_id from the model is kept as-is."""
    parsed = {
        "judgment_status": "DECIDED", "result": "FAIL",
        "misconception_signals": [{"bug_id": "bug_ref_vs_object",
                                   "direction": "FOR", "strength": "STRONG"}],
    }
    j = DiagnosticianAgent(_CapturingRouter(parsed=parsed)).judge(
        _ref_task(), "b is a separate copy of a's fields")
    assert j.misconception_signals[0].bug_id == "bug_ref_vs_object"
    assert j.misconception_signals[0].strength == SignalStrength.STRONG


# --- changed-task reconciliation (live model too-strict PARTIAL) -----------

def _changed_task(stage=1, discriminated=("bug_ref_vs_object",)):
    return TrustedTaskContext(
        task_id="changed|bug_ref_vs_object|1|x", task_version=1,
        target_concept_ids=["c_reference"], evidence_for_levels=[Level.L3],
        rubric=["Correctly identifies that a and b refer to the same object."],
        is_probe=False, is_changed_task=True,
        discriminated_bug_ids=list(discriminated),
        scenario_fingerprint="fp_test_0000001", remediation_stage=stage,
    )


def test_live_changed_task_partial_promoted_to_pass_for_clearly_correct():
    """The live model often returns PARTIAL for a clearly-correct reference
    answer, which would stall the learner in REMEDIATING forever. A clearly
    correct answer (correct cues, no wrong cues) is promoted to PASS."""
    parsed = {"judgment_status": "DECIDED", "result": "PARTIAL",
              "misconception_signals": []}
    j = DiagnosticianAgent(_CapturingRouter(parsed=parsed)).judge(
        _changed_task(), "a.getValue() returns 9 because a and b refer to the same object.")
    assert j.result == EvidenceResult.PASS


def test_live_changed_task_partial_kept_when_ambiguous():
    """When the answer is neither clearly correct nor clearly wrong, the
    model's PARTIAL stands (no override)."""
    parsed = {"judgment_status": "DECIDED", "result": "PARTIAL",
              "misconception_signals": []}
    j = DiagnosticianAgent(_CapturingRouter(parsed=parsed)).judge(
        _changed_task(), "maybe it depends on how the object is constructed")
    assert j.result == EvidenceResult.PARTIAL


def test_live_changed_task_pass_overridden_to_fail_for_clearly_wrong():
    """A model PASS on an answer that matches a known wrong-answer pattern is
    forced to FAIL so a wrong transfer cannot fake RESOLVED."""
    parsed = {"judgment_status": "DECIDED", "result": "PASS",
              "misconception_signals": []}
    j = DiagnosticianAgent(_CapturingRouter(parsed=parsed)).judge(
        _changed_task(),
        "a.getValue() returns the original value because b is a separate copy.")
    assert j.result == EvidenceResult.FAIL
    assert j.misconception_signals[0].bug_id == "bug_ref_vs_object"
