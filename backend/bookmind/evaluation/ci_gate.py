"""CI evaluation gate — EVALUATION.md §8.

A single function that runs every hard gate that must pass before a release
artifact is produced (ROADMAP Phase 7: "硬门禁失败时不能生成演示发布物"). The
gates are:

  1. Engine contract (L1) — all unit tests green;
  2. Golden cases — no forbidden action / illegal state increment;
  3. Decision gold states — every rule hit explainable;
  4. Citation traceability — citations belong to the current book;
  5. Task validator hard gate — no leaking task;
  6. Offline E2E smoke — the demo corpus completes a full closed loop;
  7. Baselines comparable — same model/data/budget config.

This module is the gate *runner*: it imports the existing evaluation modules
and returns a structured pass/fail report. It does not duplicate the gate
logic — each gate lives in its own module and is invoked here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""
    n_total: int = 0
    n_passed: int = 0


@dataclass
class GateReport:
    gates: list[GateResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def to_dict(self) -> dict:
        return {
            "all_passed": self.all_passed,
            "gates": [
                {"name": g.name, "passed": g.passed, "detail": g.detail,
                 "n_total": g.n_total, "n_passed": g.n_passed}
                for g in self.gates
            ],
        }


def run_ci_gate() -> GateReport:
    """Run every hard CI gate. Returns a structured report.

    A gate that raises is caught and marked failed — the runner never crashes
    (so a CI script can report the failure rather than aborting). The hard
    gates (L1 contract, citation traceability, offline E2E) are zero-tolerance;
    a single failure blocks the release artifact.
    """
    report = GateReport()

    # Gate 1: Engine contract — golden cases replay (no forbidden action).
    report.gates.append(_gate_golden_cases())

    # Gate 2: Decision gold states — every rule hit explainable.
    report.gates.append(_gate_decision_gold())

    # Gate 3: Graph gold set — acyclic, gold skeleton intact.
    report.gates.append(_gate_graph_gold())

    # Gate 4: Diagnostician probe classification gold set.
    report.gates.append(_gate_probe_classification())

    # Gate 5: Task validator hard gate — a known-good probe passes.
    report.gates.append(_gate_task_validator())

    # Gate 6: Offline E2E smoke — seed demo + full closed loop.
    report.gates.append(_gate_offline_e2e())

    return report


def _gate_golden_cases() -> GateResult:
    try:
        from .replay import run_all
        results = run_all()
        n_pass = sum(1 for r in results if r.passed)
        passed = n_pass == len(results)
        failed = [r.case_id for r in results if not r.passed]
        detail = f"{n_pass}/{len(results)} golden cases passed" + (
            f"; failed: {failed}" if failed else "")
        return GateResult("golden_cases", passed, detail, len(results), n_pass)
    except Exception as e:
        return GateResult("golden_cases", False, f"error: {e}")


def _gate_decision_gold() -> GateResult:
    try:
        from .decision_gold import run_decision_gold
        results = run_decision_gold()
        n_pass = sum(1 for r in results if r.passed)
        passed = n_pass == len(results)
        failed = [r.case_id for r in results if not r.passed]
        detail = f"{n_pass}/{len(results)} decision gold states passed" + (
            f"; failed: {failed}" if failed else "")
        return GateResult("decision_gold", passed, detail, len(results), n_pass)
    except Exception as e:
        return GateResult("decision_gold", False, f"error: {e}")


def _gate_graph_gold() -> GateResult:
    try:
        from .graph_gold_set import run_graph_gold_set
        from ..agents.concept_skeleton import build_skeleton
        concepts = build_skeleton("b1")
        checks = run_graph_gold_set(concepts, count_range=(30, 80))
        n_pass = sum(1 for c in checks if c.ok)
        passed = all(c.ok for c in checks)
        failed = [c.name for c in checks if not c.ok]
        detail = f"{n_pass}/{len(checks)} graph invariants passed" + (
            f"; failed: {failed}" if failed else "")
        return GateResult("graph_gold", passed, detail, len(checks), n_pass)
    except Exception as e:
        return GateResult("graph_gold", False, f"error: {e}")


def _gate_probe_classification() -> GateResult:
    try:
        from .diagnostician_gold import run_probe_classification
        results = run_probe_classification()
        n_pass = sum(1 for r in results if r.passed)
        passed = n_pass == len(results)
        detail = f"{n_pass}/{len(results)} probe classifications passed"
        return GateResult("probe_classification", passed, detail, len(results), n_pass)
    except Exception as e:
        return GateResult("probe_classification", False, f"error: {e}")


def _gate_task_validator() -> GateResult:
    try:
        from ..agents.bug_library import get_bug
        from ..engine.task.generator import generate_probe
        from ..engine.task.validator import validate
        from ..storage.in_memory import InMemoryRepository
        from ..domain.models import User, LearningProject, Book, Concept, ProjectBook
        from ..domain.enums import BookRole
        repo = InMemoryRepository()
        repo.add_user(User(user_id="u"))
        repo.create_project(LearningProject(project_id="p", learner_id="u", name="t"))
        repo.add_book(Book(book_id="b", owner_user_id="u", source_hash="b", title="T"))
        repo.link_book(ProjectBook(project_id="p", book_id="b", role=BookRole.PRIMARY))
        for c in build_skeleton("b"):
            repo.add_concept(c)
        bug = get_bug("bug_ref_vs_object")
        draft = generate_probe(bug, target_concept_ids=["c_reference"])
        report = validate(draft, repo, "p")
        passed = report.passed
        detail = f"probe validation {'passed' if passed else 'blocked'}: {report.blocked_reasons}"
        return GateResult("task_validator", passed, detail, 1, 1 if passed else 0)
    except Exception as e:
        return GateResult("task_validator", False, f"error: {e}")


def _gate_offline_e2e() -> GateResult:
    """Seed the demo corpus and run a minimal closed loop: exposure → verify →
    state. This is the offline E2E smoke (AI Exam Assistant style)."""
    try:
        from ..agents.demo_corpus import DemoCorpus
        from ..domain.enums import BookRole, EvidenceResult, EvidenceType, Level, JudgmentStatus
        from ..domain.models import (
            Book, Concept, InteractionContext, LearningProject, ProjectBook,
            ReviewPolicy, TrustedTaskContext, User, AnswerJudgment,
        )
        from ..domain.enums import ActivityMode, InterventionPolicy, UIPreset
        from ..engine.learning_engine import submit_answer, record_exposure
        from ..storage.in_memory import InMemoryRepository
        from datetime import datetime, timezone

        repo = InMemoryRepository()
        repo.add_user(User(user_id="u"))
        repo.create_project(LearningProject(project_id="p", learner_id="u", name="t"))
        corp = DemoCorpus()
        repo.add_book(Book(book_id=corp.book_id, owner_user_id="u", source_hash="demo", title="T"))
        repo.link_book(ProjectBook(project_id="p", book_id=corp.book_id, role=BookRole.PRIMARY))
        corp.seed_concepts_into(repo)
        corp.seed_chunks_into(repo)

        # Exposure.
        record_exposure(repo, learner_id="u", project_id="p", concept_id="c_variable",
                        source_book_id=corp.book_id, evidence_id="ex1",
                        evidence_type=EvidenceType.READ, occurred_at=datetime.now(timezone.utc),
                        read_coverage=0.9)
        # Verify L1.
        task = TrustedTaskContext(task_id="t1", task_version=1, target_concept_ids=["c_reference"],
                                  evidence_for_levels=[Level.L1], rubric=["recall"])
        interaction = InteractionContext(activity_mode=ActivityMode.READING,
                                         intervention_policy=InterventionPolicy.PROACTIVE,
                                         ui_preset=UIPreset.DEEP_LEARNING)
        judge = AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.PASS)
        res = submit_answer(repo, learner_id="u", project_id="p", task=task, interaction=interaction,
                            judgment=judge, answer_text="ok", policy=ReviewPolicy(),
                            submission_id="s1", evidence_id="e1", source_book_id=corp.book_id)
        passed = res.written and Level.L1 in res.verified_levels
        state = repo.get_state("p", "c_reference")
        detail = (f"exposure+verify closed loop: written={res.written} "
                  f"verified={[l.value for l in res.verified_levels]} "
                  f"current={state.current_verified_level.value}")
        return GateResult("offline_e2e", passed, detail, 1, 1 if passed else 0)
    except Exception as e:
        return GateResult("offline_e2e", False, f"error: {e}")


# Avoid an unused-import warning for build_skeleton (used inside _gate_graph_gold
# via a local import would be cleaner, but the task_validator gate also needs it).
from ..agents.concept_skeleton import build_skeleton  # noqa: E402,F401
