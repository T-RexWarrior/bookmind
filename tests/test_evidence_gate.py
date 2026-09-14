"""L1 tests: Evidence Gate — LEARNING_MODEL.md §5, PRODUCT_SPEC §8.

The Gate is the single authority for mastery verification. These pin the
hard correctness invariants:
  - READ/QUESTION/EXPLANATION never verify mastery
  - non-independent or hinted answers never verify
  - FAIL / PARTIAL never verify
  - NEEDS_REVIEW never verifies
  - concept must be in the trusted target set
"""

from __future__ import annotations

from bookmind.domain.enums import (
    EvidenceResult,
    EvidenceType,
    HintLevel,
    JudgmentStatus,
    Level,
)
from bookmind.domain.models import (
    AnswerJudgment,
    Evidence,
    TrustedTaskContext,
)
from bookmind.engine.evidence.gate import can_verify_mastery


def _task(levels=(Level.L1,), concept="c1") -> TrustedTaskContext:
    return TrustedTaskContext(
        task_id="t1",
        task_version=1,
        target_concept_ids=[concept],
        evidence_for_levels=list(levels),
        rubric=["recalls definition"],
    )


def _ev(**kw) -> Evidence:
    base = dict(
        evidence_id="e1",
        event_key="k1",
        project_id="p",
        concept_id="c1",
        source_book_id="b",
        evidence_type=EvidenceType.VERIFY,
        required_level=Level.L1,
        result=EvidenceResult.PASS,
        independent=True,
        hint_level=HintLevel.NONE,
    )
    base.update(kw)
    return Evidence(**base)


def _decided(result=EvidenceResult.PASS) -> AnswerJudgment:
    return AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=result)


def test_clean_pass_verifies_declared_levels():
    g = can_verify_mastery(_ev(), _task(levels=(Level.L1, Level.L2)), _decided())
    assert g.passed_gate
    assert g.verified_levels == [Level.L1, Level.L2]


def test_read_never_verifies():
    e = _ev(evidence_type=EvidenceType.READ, result=None)
    g = can_verify_mastery(e, _task(), _decided())
    assert not g.passed_gate
    assert "exposure-only" in " ".join(g.blocks)


def test_question_never_verifies():
    e = _ev(evidence_type=EvidenceType.QUESTION, result=None)
    assert not can_verify_mastery(e, _task(), _decided()).passed_gate


def test_explanation_never_verifies():
    e = _ev(evidence_type=EvidenceType.EXPLANATION, result=None)
    assert not can_verify_mastery(e, _task(), _decided()).passed_gate


def test_hinted_pass_does_not_verify():
    e = _ev(hint_level=HintLevel.LOW)
    g = can_verify_mastery(e, _task(), _decided())
    assert not g.passed_gate
    assert any("hint" in b for b in g.blocks)


def test_non_independent_does_not_verify():
    e = _ev(independent=False)
    assert not can_verify_mastery(e, _task(), _decided()).passed_gate


def test_fail_does_not_verify():
    e = _ev(result=EvidenceResult.FAIL)
    g = can_verify_mastery(e, _task(), _decided(EvidenceResult.FAIL))
    assert not g.passed_gate


def test_partial_does_not_verify():
    e = _ev(result=EvidenceResult.PARTIAL)
    g = can_verify_mastery(e, _task(), _decided(EvidenceResult.PARTIAL))
    assert not g.passed_gate


def test_needs_review_does_not_verify():
    j = AnswerJudgment(judgment_status=JudgmentStatus.NEEDS_REVIEW, result=None)
    g = can_verify_mastery(_ev(), _task(), j)
    assert not g.passed_gate
    assert "NEEDS_REVIEW" in " ".join(g.blocks)


def test_concept_outside_trusted_target_does_not_verify():
    e = _ev(concept_id="c_other")
    g = can_verify_mastery(e, _task(concept="c1"), _decided())
    assert not g.passed_gate
    assert any("target_concept_ids" in b for b in g.blocks)


def test_higher_task_only_verifies_declared_levels():
    """A task that only declares L3 must not silently verify L1/L2."""
    g = can_verify_mastery(_ev(), _task(levels=(Level.L3,)), _decided())
    assert g.passed_gate
    assert g.verified_levels == [Level.L3]
