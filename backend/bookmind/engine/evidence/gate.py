"""Evidence Gate — LEARNING_MODEL.md §5.

The Gate decides whether a piece of Evidence can *verify* a mastery level. It
is the single authority for the mastery upgrade invariant. READ/QUESTION/
EXPLANATION evidence never passes the Gate (they only affect exposure).

Gate rules (all must hold):

1. ``result == PASS``
2. ``independent is True``
3. ``hint_level == 0``
4. the task's ``evidence_for_levels`` declares the target level
5. the task's ``target_concept_ids`` contains the evidence concept
6. structured judgment is DECIDED (NEEDS_REVIEW produces no state increment)

A higher-order task may verify multiple levels *only if its rubric explicitly
covers the lower-level standard*. The caller checks rubric coverage; the Gate
checks the declared ``evidence_for_levels`` list.
"""

from __future__ import annotations

from ...domain.enums import EvidenceResult, EvidenceType, HintLevel, JudgmentStatus, Level
from ...domain.models import AnswerJudgment, Evidence, TrustedTaskContext
from pydantic import BaseModel, Field


GATE_PASSED_LEVELS: tuple[Level, ...] = (Level.L1, Level.L2, Level.L3, Level.L4)


class GateDecision(BaseModel):
    verified_levels: list[Level] = Field(default_factory=list)
    passed_gate: bool = False
    blocks: list[str] = Field(default_factory=list)


def can_verify_mastery(
    evidence: Evidence,
    task: TrustedTaskContext,
    judgment: AnswerJudgment | None,
) -> GateDecision:
    """Return which (if any) levels this evidence verifies and why not."""
    blocks: list[str] = []

    # READ / QUESTION / EXPLANATION only affect exposure — never mastery.
    if evidence.evidence_type in (EvidenceType.READ, EvidenceType.QUESTION, EvidenceType.EXPLANATION):
        blocks.append("exposure-only evidence type")
        return GateDecision(verified_levels=[], passed_gate=False, blocks=blocks)

    if judgment is not None and judgment.judgment_status != JudgmentStatus.DECIDED:
        blocks.append("judgment not DECIDED (NEEDS_REVIEW)")
        return GateDecision(verified_levels=[], passed_gate=False, blocks=blocks)

    if evidence.result != EvidenceResult.PASS:
        blocks.append(f"result={evidence.result.value if evidence.result else 'None'} != PASS")
    if not evidence.independent:
        blocks.append("not independent")
    if evidence.hint_level != HintLevel.NONE:
        blocks.append(f"hint_level={evidence.hint_level.value} > 0")

    # Concept must be in the trusted task's target set.
    if evidence.concept_id not in task.target_concept_ids:
        blocks.append("concept not in trusted target_concept_ids")

    if blocks:
        return GateDecision(verified_levels=[], passed_gate=False, blocks=blocks)

    # Everything passed — the levels this evidence can verify are exactly those
    # the task declared AND that are >= L1.
    verified = [lvl for lvl in task.evidence_for_levels if lvl in GATE_PASSED_LEVELS]
    return GateDecision(verified_levels=verified, passed_gate=True, blocks=[])
