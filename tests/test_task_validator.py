"""Phase 5 tests: shared Task Validator — ARCHITECTURE §3.5, OPEN_SOURCES §7.

Covers the "所有正式题通过共享 Task Validator" acceptance: each gate stage can
pass and fail; dedup catches duplicate changed-task fingerprints; probe quality
rejects probes that don't discriminate a known bug.
"""

from __future__ import annotations

from bookmind.domain.enums import Level
from bookmind.domain.models import TaskDraft
from bookmind.engine.task.validator import fingerprint, validate
from bookmind.storage.in_memory import InMemoryRepository


def _repo():
    return InMemoryRepository()


def _good_probe_draft(**over):
    base = dict(
        task_id="probe1", target_concept_ids=["c_reference"],
        evidence_for_levels=[Level.L2], rubric=["identifies aliasing"],
        prompt_text="Given Box a = new Box(1); Box b = a; b.setValue(9); what is a.getValue() and why?",
        is_probe=True, discriminated_bug_ids=["bug_ref_vs_object"],
    )
    base.update(over)
    return TaskDraft(**base)


# --- Schema ----------------------------------------------------------------

def test_schema_rejects_empty_targets():
    r = validate(_good_probe_draft(target_concept_ids=[]), _repo(), "p1")
    assert not r.passed
    assert any("schema" in b for b in r.blocked_reasons)


def test_schema_rejects_empty_rubric():
    r = validate(_good_probe_draft(rubric=[]), _repo(), "p1")
    assert not r.passed


def test_schema_rejects_l0_level():
    r = validate(_good_probe_draft(evidence_for_levels=[Level.L0]), _repo(), "p1")
    assert not r.passed


# --- A passing probe ------------------------------------------------------

def test_valid_probe_passes_all_stages():
    r = validate(_good_probe_draft(), _repo(), "p1")
    assert r.passed, r.blocked_reasons
    assert r.trusted is not None
    assert r.trusted.is_probe is True
    assert r.trusted.discriminated_bug_ids == ["bug_ref_vs_object"]
    # execution check is skipped offline.
    names = {c.name: c for c in r.checks}
    assert names["execution"].skipped is True


# --- Grounding -------------------------------------------------------------

def test_grounding_rejects_unknown_chunk():
    from bookmind.domain.source_ref import SourceRef
    draft = _good_probe_draft(source_refs=[SourceRef(document_id="d", chunk_id="nope", physical_page=1)])
    r = validate(draft, _repo(), "p1")
    assert not r.passed
    assert any("grounding" in b for b in r.blocked_reasons)


def test_grounding_passes_with_no_source_refs():
    r = validate(_good_probe_draft(), _repo(), "p1")
    names = {c.name: c for c in r.checks}
    assert names["grounding"].passed is True


# --- Level alignment -------------------------------------------------------

def test_level_alignment_rejects_short_recall_prompt_for_l4():
    draft = _good_probe_draft(
        evidence_for_levels=[Level.L4], prompt_text="Recall.",
    )
    r = validate(draft, _repo(), "p1")
    assert not r.passed
    assert any("level" in b for b in r.blocked_reasons)


def test_level_alignment_accepts_explanatory_l4():
    draft = _good_probe_draft(
        evidence_for_levels=[Level.L4],
        prompt_text="Explain how reference aliasing affects a multi-step mutation across methods.",
    )
    r = validate(draft, _repo(), "p1")
    # May fail other stages, but not the level stage.
    names = {c.name: c for c in r.checks}
    assert names["level"].passed is True


# --- Answerability ---------------------------------------------------------

def test_answerability_rejects_mc_without_answer():
    draft = _good_probe_draft(distractors=["copy", "alias"], expected_answer="")
    r = validate(draft, _repo(), "p1")
    assert not r.passed
    assert any("answerability" in b for b in r.blocked_reasons)


def test_answerability_rejects_answer_in_distractors():
    draft = _good_probe_draft(distractors=["copy", "alias"], expected_answer="alias")
    r = validate(draft, _repo(), "p1")
    assert not r.passed


# --- Probe quality ---------------------------------------------------------

def test_probe_quality_rejects_unknown_bug():
    draft = _good_probe_draft(discriminated_bug_ids=["bug_does_not_exist"])
    r = validate(draft, _repo(), "p1")
    assert not r.passed
    assert any("probe" in b for b in r.blocked_reasons)


def test_probe_quality_rejects_no_discriminated_bug():
    draft = _good_probe_draft(discriminated_bug_ids=[])
    r = validate(draft, _repo(), "p1")
    assert not r.passed


# --- Dedup -----------------------------------------------------------------

def test_dedup_rejects_duplicate_changed_task_fingerprint():
    repo = _repo()
    from bookmind.domain.enums import EvidenceResult, EvidenceType
    from bookmind.domain.models import Evidence
    fp = "fp1234567890abcd"
    repo.append_evidence(Evidence(
        evidence_id="e1", event_key="k1", project_id="p1", concept_id="c_reference",
        source_book_id="b", evidence_type=EvidenceType.CHANGED_TASK,
        required_level="L3", result=EvidenceResult.PASS, independent=True,
        task_id="ct1", scenario_fingerprint=fp,
    ))
    draft = TaskDraft(
        task_id="ct2", target_concept_ids=["c_reference"],
        evidence_for_levels=[Level.L3], rubric=["identifies aliasing"],
        prompt_text="A near-transfer scenario.", is_changed_task=True,
        discriminated_bug_ids=["bug_ref_vs_object"], scenario_fingerprint=fp,
        remediation_stage=1,
    )
    r = validate(draft, repo, "p1")
    assert not r.passed
    assert any("dedup" in b for b in r.blocked_reasons)


def test_dedup_allows_different_fingerprints():
    repo = _repo()
    from bookmind.domain.enums import EvidenceResult, EvidenceType
    from bookmind.domain.models import Evidence
    repo.append_evidence(Evidence(
        evidence_id="e1", event_key="k1", project_id="p1", concept_id="c_reference",
        source_book_id="b", evidence_type=EvidenceType.CHANGED_TASK,
        required_level="L3", result=EvidenceResult.PASS, independent=True,
        task_id="ct1", scenario_fingerprint="aaa1111111111111",
    ))
    draft = TaskDraft(
        task_id="ct2", target_concept_ids=["c_reference"],
        evidence_for_levels=[Level.L3], rubric=["identifies aliasing"],
        prompt_text="A far-transfer scenario.", is_changed_task=True,
        discriminated_bug_ids=["bug_ref_vs_object"], scenario_fingerprint="bbb2222222222222",
        remediation_stage=2,
    )
    r = validate(draft, repo, "p1")
    names = {c.name: c for c in r.checks}
    assert names["dedup"].passed is True


def test_fingerprint_includes_stage():
    fp1 = fingerprint("same prompt", ["c1"], stage=1)
    fp2 = fingerprint("same prompt", ["c1"], stage=2)
    assert fp1 != fp2


def _dedup_draft(fp: str, stage: int = 1) -> TaskDraft:
    return TaskDraft(
        task_id="ct_retry", target_concept_ids=["c_reference"],
        evidence_for_levels=[Level.L3], rubric=["identifies aliasing"],
        prompt_text="A near-transfer scenario.", is_changed_task=True,
        discriminated_bug_ids=["bug_ref_vs_object"], scenario_fingerprint=fp,
        remediation_stage=stage,
    )


def _dedup_evidence(fp: str, result) -> "Evidence":
    from bookmind.domain.enums import EvidenceResult, EvidenceType
    from bookmind.domain.models import Evidence
    return Evidence(
        evidence_id="e_prev", event_key="k_prev", project_id="p1",
        concept_id="c_reference", source_book_id="b",
        evidence_type=EvidenceType.CHANGED_TASK, required_level="L3",
        result=result, independent=True, task_id="ct_prev",
        scenario_fingerprint=fp,
    )


def test_dedup_allows_retry_after_partial():
    """A PARTIAL changed task keeps the learner in REMEDIATING so they can retry
    the same scenario — the dedup gate must not block the re-attempt."""
    repo = _repo()
    from bookmind.domain.enums import EvidenceResult
    fp = "fp_partial_retry_001"
    repo.append_evidence(_dedup_evidence(fp, EvidenceResult.PARTIAL))
    r = validate(_dedup_draft(fp), repo, "p1")
    names = {c.name: c for c in r.checks}
    assert names["dedup"].passed is True


def test_dedup_allows_retry_after_fail():
    """A FAIL changed task resets to CONFIRMED; retrying the same scenario is
    legitimate and must not be dedup-blocked."""
    repo = _repo()
    from bookmind.domain.enums import EvidenceResult
    fp = "fp_fail_retry_000001"
    repo.append_evidence(_dedup_evidence(fp, EvidenceResult.FAIL))
    r = validate(_dedup_draft(fp), repo, "p1")
    names = {c.name: c for c in r.checks}
    assert names["dedup"].passed is True


def test_dedup_still_blocks_after_pass():
    """Only a previously PASS-ing scenario is a true duplicate (would let the
    learner fake RESOLVED by re-submitting the same passed scenario)."""
    repo = _repo()
    from bookmind.domain.enums import EvidenceResult
    fp = "fp_pass_block_000001"
    repo.append_evidence(_dedup_evidence(fp, EvidenceResult.PASS))
    r = validate(_dedup_draft(fp), repo, "p1")
    assert not r.passed
    assert any("dedup" in b for b in r.blocked_reasons)
