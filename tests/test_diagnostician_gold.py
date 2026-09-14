"""Phase 5 tests: Diagnostician human gold answer set — EVALUATION §3.1/§3.3.

Covers: the gold set has the required 8 categories; the classification test
passes on the fixed human answer set (LEARNING_MODEL §9 "在固定人工回答集上
通过分类测试"); the gold judgments are internally consistent.
"""

from __future__ import annotations

from bookmind.evaluation.diagnostician_gold import (
    ALL_CATEGORIES, DIAGNOSTICIAN_GOLD, run_probe_classification,
)
from bookmind.agents.bug_library import BUG_LIBRARY


def test_gold_set_covers_eight_categories_for_primary_bug():
    cats = {gc.category for gc in DIAGNOSTICIAN_GOLD if gc.bug_id == "bug_ref_vs_object"}
    assert cats == set(ALL_CATEGORIES)


def test_gold_set_covers_all_five_bugs():
    bugs = {gc.bug_id for gc in DIAGNOSTICIAN_GOLD}
    assert bugs == set(BUG_LIBRARY.keys())


def test_every_gold_case_has_valid_task_and_judgment():
    for gc in DIAGNOSTICIAN_GOLD:
        assert gc.task.target_concept_ids
        assert gc.task.evidence_for_levels
        assert gc.gold_judgment.judgment_status is not None
        # A DECIDED judgment must carry a result.
        from bookmind.domain.enums import JudgmentStatus
        if gc.gold_judgment.judgment_status == JudgmentStatus.DECIDED:
            assert gc.gold_judgment.result is not None


def test_classification_test_passes_on_gold_set():
    results = run_probe_classification()
    assert results, "no classification cases were run"
    failures = [r for r in results if not r.passed]
    assert not failures, (
        "probe classification failed on the gold set: "
        + "; ".join(f"{r.case_id}: expected {r.expected}, got {r.got}" for r in failures)
    )


def test_classification_test_covers_wrong_colloquial_code_categories():
    results = run_probe_classification()
    cats = {r.category for r in results}
    # The classification test should at least exercise the wrong/colloquial/code_nl cases.
    assert "typical_wrong" in cats
    assert "colloquial" in cats
    assert "code_nl" in cats


def test_gold_set_size_is_reasonable():
    # 8 for the primary bug + 3 × 4 for the others = 20 cases.
    assert len(DIAGNOSTICIAN_GOLD) >= 20
