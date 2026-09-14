"""L3 tests: deterministic learner simulator + closed-loop benchmark.

Pins EVALUATION §4 reproducibility: same profile + seed → same behaviour, and
the §6 metrics are computed correctly. Also checks the §5 acceptance: results
are reproducible, and the simulator is deterministic.
"""

from __future__ import annotations

from bookmind.domain.enums import EvidenceResult, Level
from bookmind.evaluation.learner_simulator import (
    LearnerProfile,
    SimulatedLearner,
    default_profiles,
)
from bookmind.evaluation.benchmark import (
    BenchmarkConfig,
    run_benchmark,
    compute_metrics,
    SystemRunResult,
)


# --- simulator determinism --------------------------------------------------

def test_simulator_is_deterministic_same_seed():
    prof = LearnerProfile(
        profile_id="t", description="t",
        mastery_gt={"c1": Level.L2}, misconception_gt={},
        slip_rate=0.3, seed=999,
    )
    a = SimulatedLearner(prof)
    b = SimulatedLearner(prof)
    # 20 responses — must be identical with the same seed.
    for _ in range(20):
        ra = a.respond(concept_id="c1", required_level=Level.L2, independent=True)
        rb = b.respond(concept_id="c1", required_level=Level.L2, independent=True)
        assert ra.result == rb.result
        assert ra.used_hint == rb.used_hint


def test_simulator_passes_when_gt_meets_level():
    prof = LearnerProfile(
        profile_id="t", description="t",
        mastery_gt={"c1": Level.L3}, misconception_gt={},
        slip_rate=0.0, seed=1,
    )
    learner = SimulatedLearner(prof)
    # No slip → always PASS at L2 (gt >= level).
    for _ in range(5):
        r = learner.respond(concept_id="c1", required_level=Level.L2, independent=True)
        assert r.result == EvidenceResult.PASS
        assert not r.used_hint


def test_simulator_fails_when_gt_below_level():
    prof = LearnerProfile(
        profile_id="t", description="t",
        mastery_gt={"c1": Level.L1}, misconception_gt={},
        slip_rate=0.0, hint_dependence=0.0, seed=1,
    )
    learner = SimulatedLearner(prof)
    # gt=L1, asked L3 → far below → FAIL.
    r = learner.respond(concept_id="c1", required_level=Level.L3, independent=True)
    assert r.result == EvidenceResult.FAIL


def test_simulator_partial_when_close():
    prof = LearnerProfile(
        profile_id="t", description="t",
        mastery_gt={"c1": Level.L1}, misconception_gt={},
        slip_rate=0.0, hint_dependence=0.0, seed=1,
    )
    learner = SimulatedLearner(prof)
    # gt=L1, asked L2 → within 1 level → PARTIAL.
    r = learner.respond(concept_id="c1", required_level=Level.L2, independent=True)
    assert r.result == EvidenceResult.PARTIAL


def test_simulator_probe_reveals_active_bug():
    prof = LearnerProfile(
        profile_id="t", description="t",
        mastery_gt={"c_reference": Level.L1},
        misconception_gt={"c_reference": "bug_ref_vs_object"},
        seed=1,
    )
    learner = SimulatedLearner(prof)
    r = learner.respond(concept_id="c_reference", required_level=Level.L2,
                        independent=True, is_probe=True,
                        discriminated_bug_id="bug_ref_vs_object")
    assert r.result == EvidenceResult.FAIL
    assert r.bug_id == "bug_ref_vs_object"
    assert r.signal_direction == "FOR"
    assert r.signal_strength == "STRONG"


def test_simulator_probe_against_different_bug():
    prof = LearnerProfile(
        profile_id="t", description="t",
        mastery_gt={"c_reference": Level.L1},
        misconception_gt={"c_reference": "bug_ref_vs_object"},
        seed=1,
    )
    learner = SimulatedLearner(prof)
    # Probing a different bug — learner's answer discriminates AGAINST it.
    r = learner.respond(concept_id="c_reference", required_level=Level.L2,
                        independent=True, is_probe=True,
                        discriminated_bug_id="bug_eq_vs_equals")
    assert r.signal_direction == "AGAINST"


def test_default_profiles_cover_axes():
    profiles = default_profiles()
    assert len(profiles) >= 5
    # At least one with a misconception, one without.
    has_bug = any(any(v != "none" for v in p.misconception_gt.values()) for p in profiles)
    no_bug = any(not p.misconception_gt or all(v == "none" for v in p.misconception_gt.values()) for p in profiles)
    assert has_bug and no_bug


# --- benchmark reproducibility & metrics ------------------------------------

def test_benchmark_is_reproducible():
    cfg = BenchmarkConfig(budget=20, verification_window=10,
                          systems=["bookmind", "b0_basic_tutor"])
    r1 = run_benchmark(config=cfg)
    r2 = run_benchmark(config=cfg)
    assert r1 == r2  # exact equality — deterministic


def test_benchmark_runs_all_systems():
    cfg = BenchmarkConfig(budget=15, verification_window=8,
                          systems=["bookmind", "b0_basic_tutor", "b1_pdf_rag", "b2_fixed_flow"])
    report = run_benchmark(config=cfg)
    assert set(report["summary"].keys()) == {"bookmind", "b0_basic_tutor", "b1_pdf_rag", "b2_fixed_flow"}
    # Every system has metrics in [0, 1] range.
    for sys_name, s in report["summary"].items():
        assert 0.0 <= s["strict_accuracy"] <= 1.0
        assert 0.0 <= s["fmr"] <= 1.0
        assert 0.0 <= s["coverage"] <= 1.0


def test_b0_judgement_uses_last_answer():
    """B0's mastery is just the last quiz result (L1 pass / L0 fail)."""
    from bookmind.evaluation.benchmark import _run_b0_basic
    prof = LearnerProfile(
        profile_id="t", description="t",
        mastery_gt={"c_variable": Level.L2, "c_polymorphism": Level.L0},
        misconception_gt={}, seed=1,
    )
    res = _run_b0_basic(prof, concept_ids=["c_variable", "c_polymorphism"],
                        budget=10, verification_window=5)
    # c_variable (gt L2) should pass L1 quizzes → judged L1.
    assert res.final_judgement["c_variable"] == Level.L1
    # c_polymorphism (gt L0) should fail → judged L0.
    assert res.final_judgement["c_polymorphism"] == Level.L0


def test_bookmind_uses_real_evidence_gate():
    """BookMind's judgement comes from the real Evidence Gate, not last-answer.
    A concept the learner knows (gt L2) should reach at least L1 via the Gate
    after enough independent passes."""
    from bookmind.evaluation.benchmark import _run_bookmind
    prof = LearnerProfile(
        profile_id="t", description="strong",
        mastery_gt={"c_variable": Level.L3, "c_reference": Level.L2},
        misconception_gt={}, slip_rate=0.0, seed=1,
    )
    res = _run_bookmind(prof, concept_ids=["c_variable", "c_reference"],
                        budget=30, verification_window=15)
    # At least one of the known concepts should be verified L1+.
    judged = [res.final_judgement[c] for c in ("c_variable", "c_reference")]
    assert any(_level_rank(j) >= 1 for j in judged)


def test_metrics_fmr_zero_when_no_false_mastery():
    """A system that never over-judges should have FMR=0."""
    res = SystemRunResult(system_name="x", profile_id="t")
    res.final_judgement = {"c1": Level.L0, "c2": Level.L1}
    res.denominator_concepts = {"c1", "c2"}
    # No false mastery, no censored.
    prof = LearnerProfile(profile_id="t", description="t",
                          mastery_gt={"c1": Level.L0, "c2": Level.L1}, misconception_gt={}, seed=1)
    m = compute_metrics(res, prof, total_concepts=2)
    assert m.fmr == 0.0
    assert m.n_censored == 0


def test_metrics_reports_censored():
    """A concept judged L2+ with no re-verification chance is censored."""
    res = SystemRunResult(system_name="x", profile_id="t")
    res.final_judgement = {"c1": Level.L2}
    # c1 never reverified → censored.
    prof = LearnerProfile(profile_id="t", description="t",
                          mastery_gt={"c1": Level.L2}, misconception_gt={}, seed=1)
    m = compute_metrics(res, prof, total_concepts=1)
    assert m.n_censored == 1


def _level_rank(level: Level) -> int:
    return {Level.L0: 0, Level.L1: 1, Level.L2: 2, Level.L3: 3, Level.L4: 4}[level]
