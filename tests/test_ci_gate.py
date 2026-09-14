"""Tests: CI evaluation gate — EVALUATION.md §8.

The gate runner must itself be correct: every hard gate passes in a clean
state, and a gate that fails is reported (not silently dropped).
"""

from __future__ import annotations

from bookmind.evaluation.ci_gate import run_ci_gate


def test_ci_gate_all_pass_clean():
    report = run_ci_gate()
    assert report.all_passed, (
        "CI gate failures:\n  "
        + "\n  ".join(f"{g.name}: {g.detail}" for g in report.gates if not g.passed)
    )


def test_ci_gate_runs_six_gates():
    report = run_ci_gate()
    names = [g.name for g in report.gates]
    # The six hard gates.
    assert "golden_cases" in names
    assert "decision_gold" in names
    assert "graph_gold" in names
    assert "probe_classification" in names
    assert "task_validator" in names
    assert "offline_e2e" in names
    assert len(report.gates) == 6


def test_ci_gate_report_serialisable():
    report = run_ci_gate()
    d = report.to_dict()
    assert "all_passed" in d
    assert "gates" in d
    assert all("name" in g and "passed" in g for g in d["gates"])


def test_ci_gate_offline_e2e_passes():
    """The offline E2E smoke gate must pass — it seeds the demo corpus and
    runs exposure + verify (ROADMAP Phase 7 CI 门禁 #5)."""
    report = run_ci_gate()
    e2e = next(g for g in report.gates if g.name == "offline_e2e")
    assert e2e.passed
    assert "written=True" in e2e.detail
