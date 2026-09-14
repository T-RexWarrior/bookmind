"""Task Validator — ARCHITECTURE.md §3.5, OPEN_SOURCE_REFERENCES.md §7.

The shared, deterministic-first gate every formal task must pass before it can
become an immutable :class:`~bookmind.domain.models.TrustedTaskContext` and
produce Evidence. The pipeline (OPEN_SOURCE_REFERENCES §7):

    1. Schema            — required fields, valid levels
    2. Source Grounding  — cited chunks belong to the project's book
    3. Level Alignment   — declared levels match the task's cognitive demand
    4. Answerability     — has an answer or a rubric that can judge open answers
    5. Probe/Distractor  — probes discriminate a known bug; distractors map to bugs
    6. Deduplication     — not a surface rewrite of an existing task
    7. Execution Check   — deterministic re-check of code/numeric answers (offline: skip)

Deterministic checks run first and always. LLM-aid checks degrade to ``skipped``
when no live model is available (offline mode) rather than blocking. A failing
check means the task cannot produce Evidence — it is reported, not silently fixed.

The validator never sets ``required_level`` / ``hint_level`` / ``independent``;
those are derived by the Learning Engine from the trusted interaction context.
"""

from __future__ import annotations

import hashlib

from ...domain.enums import EvidenceResult, EvidenceType, Level
from ...domain.models import (
    CheckResult,
    TaskDraft,
    TrustedTaskContext,
    ValidationReport,
)
from ...storage.protocols import Repository


# Levels that may be declared as evidence. L0 never produces mastery evidence.
_VERIFYABLE_LEVELS = {Level.L1, Level.L2, Level.L3, Level.L4}


def fingerprint(prompt_text: str, target_concept_ids: list[str], *, stage: int = 0) -> str:
    """A stable scenario fingerprint for dedup.

    Includes the stage so the two changed tasks of a remediation (near/far
    transfer) never collide even if their prompts are similar.
    """
    raw = f"{prompt_text.strip().lower()}|{','.join(sorted(target_concept_ids))}|{stage}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def validate(draft: TaskDraft, repo: Repository, project_id: str) -> ValidationReport:
    """Run the 7-stage gate. Returns a report; ``passed`` is True iff all
    non-skipped checks passed. On success ``report.trusted`` is the immutable
    :class:`TrustedTaskContext`.
    """
    checks: list[CheckResult] = []
    blocked: list[str] = []

    # Stage 1 — Schema.
    ok, detail = _check_schema(draft)
    checks.append(CheckResult(name="schema", passed=ok, detail=detail))
    if not ok:
        blocked.append(f"schema: {detail}")

    # Stage 2 — Source Grounding.
    ok, detail = _check_grounding(draft, repo, project_id)
    checks.append(CheckResult(name="grounding", passed=ok, detail=detail))
    if not ok:
        blocked.append(f"grounding: {detail}")

    # Stage 3 — Level Alignment.
    ok, detail = _check_level_alignment(draft, repo, project_id)
    checks.append(CheckResult(name="level", passed=ok, detail=detail))
    if not ok:
        blocked.append(f"level: {detail}")

    # Stage 4 — Answerability.
    ok, detail = _check_answerability(draft)
    checks.append(CheckResult(name="answerability", passed=ok, detail=detail))
    if not ok:
        blocked.append(f"answerability: {detail}")

    # Stage 5 — Probe / Distractor quality.
    ok, detail = _check_probe_quality(draft)
    checks.append(CheckResult(name="probe", passed=ok, detail=detail))
    if not ok:
        blocked.append(f"probe: {detail}")

    # Stage 6 — Deduplication.
    ok, detail = _check_dedup(draft, repo, project_id)
    checks.append(CheckResult(name="dedup", passed=ok, detail=detail))
    if not ok:
        blocked.append(f"dedup: {detail}")

    # Stage 7 — Execution Check (offline: skip; no deterministic solver wired yet).
    checks.append(CheckResult(name="execution", passed=True, skipped=True,
                              detail="no deterministic solver available; skipped"))

    passed = not blocked
    trusted = _to_trusted(draft) if passed else None
    return ValidationReport(passed=passed, checks=checks, blocked_reasons=blocked, trusted=trusted)


# --- stage implementations ----------------------------------------------------

def _check_schema(draft: TaskDraft) -> tuple[bool, str]:
    if not draft.target_concept_ids:
        return False, "target_concept_ids is empty"
    if not draft.evidence_for_levels:
        return False, "evidence_for_levels is empty"
    bad = [lvl for lvl in draft.evidence_for_levels if lvl not in _VERIFYABLE_LEVELS]
    if bad:
        return False, f"non-verifyable levels: {[l.value for l in bad]}"
    if not draft.rubric:
        return False, "rubric is empty"
    if not draft.prompt_text:
        return False, "prompt_text is empty"
    return True, "ok"


def _check_grounding(draft: TaskDraft, repo: Repository, project_id: str) -> tuple[bool, str]:
    if not draft.source_refs:
        return True, "no source_refs; template/fallback task (skipped)"
    allowed_chunk_ids = {c.chunk_id for c in repo.chunks_for_project(project_id)}
    for ref in draft.source_refs:
        if ref.chunk_id and ref.chunk_id not in allowed_chunk_ids:
            return False, f"chunk {ref.chunk_id} not in project {project_id} scope"
    return True, "ok"


def _check_level_alignment(draft: TaskDraft, repo: Repository, project_id: str) -> tuple[bool, str]:
    """A task declaring L4 (transfer) should not be a pure recall prompt.

    We use a soft heuristic: an L4/L3 task whose prompt is a single short recall
    phrase (< 12 chars, no verb) is suspicious. This is deliberately permissive
    — the gate must not block legitimate tasks.
    """
    high = [lvl for lvl in draft.evidence_for_levels if lvl in (Level.L3, Level.L4)]
    if not high:
        return True, "ok"
    prompt = draft.prompt_text.strip()
    # Very short recall-style prompts for a transfer task are a misalignment.
    if len(prompt) < 12 and not any(v in prompt.lower() for v in ("why", "how", "explain", "为何", "如何", "解释", "比较")):
        return False, f"high-level task ({[l.value for l in high]}) has a recall-length prompt"
    return True, "ok"


def _check_answerability(draft: TaskDraft) -> tuple[bool, str]:
    has_answer = bool(draft.expected_answer)
    has_rubric = bool(draft.rubric)
    if not has_answer and not has_rubric:
        return False, "neither expected_answer nor rubric provided"
    if draft.distractors:
        # Multiple-choice: need a single correct answer to mark.
        if not has_answer:
            return False, "multiple-choice task (has distractors) needs expected_answer"
        if draft.expected_answer in draft.distractors:
            return False, "expected_answer appears among distractors"
        if len(set(draft.distractors)) != len(draft.distractors):
            return False, "duplicate distractors"
    return True, "ok"


def _check_probe_quality(draft: TaskDraft) -> tuple[bool, str]:
    from ...agents.bug_library import BUG_LIBRARY

    if draft.is_probe:
        if not draft.discriminated_bug_ids:
            return False, "probe declares no discriminated_bug_ids"
        for bid in draft.discriminated_bug_ids:
            if bid not in BUG_LIBRARY:
                return False, f"probe references unknown bug_id {bid}"
        # A probe must discriminate between >= 2 competing hypotheses.
        for bid in draft.discriminated_bug_ids:
            bug = BUG_LIBRARY[bid]
            if len(bug.competing_hypotheses) < 2:
                return False, f"bug {bid} has < 2 competing hypotheses; not a discriminating probe"
        return True, "ok"
    return True, "not a probe (skipped probe-specific checks)"


def _check_dedup(draft: TaskDraft, repo: Repository, project_id: str) -> tuple[bool, str]:
    """Changed tasks must not reuse an already-passed scenario fingerprint.

    The dedup backstop prevents a learner from earning both remediation
    milestones (→ RESOLVED) by submitting the *same* scenario twice. It must
    NOT block a re-attempt after a PARTIAL or FAIL — the state machine keeps
    the learner in REMEDIATING on a PARTIAL and resets on a FAIL precisely so
    they can retry, and the scenario is often the same near-transfer template
    (the live model rephrases the wording but the fingerprint is template-
    derived). Only a previously PASS-ing scenario is a true duplicate: the
    state machine also de-duplicates pass fingerprints, so this is the safety
    net, not the sole authority.

    For ordinary tasks we only dedup on an exact fingerprint match; the gate is
    intentionally lenient so it does not block legitimate paraphrases.
    """
    fp = draft.scenario_fingerprint
    if fp is None and draft.is_changed_task:
        fp = fingerprint(draft.prompt_text, draft.target_concept_ids, stage=draft.remediation_stage)
    if fp is None:
        return True, "no fingerprint; ordinary task (lenient)"
    # Scan existing evidence for a PASS-ing changed task with the same fingerprint.
    for e in repo.evidence_for_project(project_id):
        if (e.scenario_fingerprint and e.scenario_fingerprint == fp
                and e.evidence_type == EvidenceType.CHANGED_TASK
                and e.result == EvidenceResult.PASS):
            return False, f"duplicate scenario fingerprint {fp} (already passed)"
    return True, "ok"


def _to_trusted(draft: TaskDraft) -> TrustedTaskContext:
    fp = draft.scenario_fingerprint
    if fp is None and draft.is_changed_task:
        fp = fingerprint(draft.prompt_text, draft.target_concept_ids, stage=draft.remediation_stage)
    return TrustedTaskContext(
        task_id=draft.task_id,
        task_version=draft.task_version,
        target_concept_ids=list(draft.target_concept_ids),
        evidence_for_levels=list(draft.evidence_for_levels),
        rubric=list(draft.rubric),
        source_refs=list(draft.source_refs),
        scenario_fingerprint=fp,
        is_probe=draft.is_probe,
        discriminated_bug_ids=list(draft.discriminated_bug_ids),
        is_changed_task=draft.is_changed_task,
        remediation_stage=draft.remediation_stage,
    )
