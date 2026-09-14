"""Offline demo runner — proves the full BookMind closed loop with no network.

Roadmap Phase 1/2 acceptance: "完整闭环可重复运行，不依赖现场 PDF 解析、外部网络或
实时模型". This script seeds the offline Java demo corpus, runs a textbook Q&A,
performs a mastery verification, and prints the resulting state — all offline.

Usage::

    python scripts/run_demo.py            # offline (no API key)
    USTC_LLM_API_KEY=sk-... python scripts/run_demo.py   # live model enhancement

Run from the BookMind project root.
"""

from __future__ import annotations

import os
import sys

# Make the backend importable when run from the project root.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from bookmind.agents.demo_corpus import DemoCorpus
from bookmind.domain.enums import BookRole, EvidenceResult, Level
from bookmind.domain.models import (
    Book, InteractionContext, LearningProject, ProjectBook, ReviewPolicy,
    TrustedTaskContext, User,
)
from bookmind.domain.enums import ActivityMode, InterventionPolicy, UIPreset
from bookmind.engine.learning_engine import submit_answer
from bookmind.llm.router import ModelRouter, RouterConfig
from bookmind.services import BookQAService
from bookmind.storage.in_memory import InMemoryRepository


def main() -> int:
    live = bool(os.environ.get("USTC_LLM_API_KEY"))
    print("=" * 60)
    print("学迹 / BookMind — 离线 demo" + ("（实时模型增强）" if live else "（离线模式）"))
    print("=" * 60)

    repo = InMemoryRepository()
    repo.add_user(User(user_id="u1", display_name="Ada"))
    repo.create_project(LearningProject(project_id="p1", learner_id="u1", name="Java OOP",
                                        goal="掌握 Java 核心概念"))
    corp = DemoCorpus()
    repo.add_book(Book(book_id=corp.book_id, owner_user_id="u1", source_hash="demo", title="Java Core (demo)"))
    repo.link_book(ProjectBook(project_id="p1", book_id=corp.book_id, role=BookRole.PRIMARY))
    corp.seed_concepts_into(repo)
    corp.seed_chunks_into(repo)

    router = ModelRouter(RouterConfig(live=live))
    retriever = corp.build_retriever(router)
    repo.set_retriever("p1", retriever)
    svc = BookQAService(repo, router)

    print(f"\n[seed] {len(corp.concepts)} concepts, {len(corp.chunks)} chunks indexed")

    # 0. Book Mapping — extend the gold skeleton to ~50 concepts (Phase 3).
    print("\n--- 0. Book Mapping (LLM 概念图谱扩展) ---")
    from bookmind.services import BookMappingService
    from bookmind.evaluation.graph_gold_set import run_graph_gold_set
    map_svc = BookMappingService(repo, router)
    report = map_svc.map_book(
        project_id="p1", learner_id="u1", book_id=corp.book_id,
        parsed_document=corp.parsed_document, chunks=corp.chunks, graph_key="demo_g1",
    )
    concepts = repo.concepts_for_book(corp.book_id)
    print(f"gold={report.gold_concepts} new={report.new_concepts} total={report.total_concepts} "
          f"prereq_edges={report.total_prereq_edges} dropped={len(report.dropped_edges)} "
          f"fallback_sections={report.fallback_sections}")
    # Run the graph quality gold set.
    checks = run_graph_gold_set(concepts, count_range=(50, 80))
    for chk in checks:
        flag = "✓" if chk.ok else "✗"
        print(f"  {flag} {chk.name}: {chk.detail}")
    assert all(c.ok for c in checks), "graph quality gold set failed"

    # 1. Textbook Q&A.
    print("\n--- 1. 教材问答 (Hybrid RAG + 引用) ---")
    questions = [
        "== 和 equals 的区别是什么？",
        "为什么重写 equals 必须重写 hashCode？",
    ]
    for q in questions:
        ans = svc.ask(project_id="p1", learner_id="u1", question=q)
        print(f"\nQ: {q}")
        print(f"A: {ans.answer_text[:200]}")
        print(f"   grounded={ans.grounded} chunks={ans.chunk_ids[:2]} citations={len(ans.citations)}")

    # 2. Mastery verification (independent L1 pass).
    print("\n--- 2. 掌握验证 (Evidence Gate, L1) ---")
    task = TrustedTaskContext(
        task_id="verify_l1_ref", task_version=1,
        target_concept_ids=["c_reference"], evidence_for_levels=[Level.L1],
        rubric=["recalls that a reference stores an address"],
    )
    interaction = InteractionContext(
        activity_mode=ActivityMode.READING, intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING, hints_issued=0,
    )
    from bookmind.domain.enums import JudgmentStatus
    from bookmind.domain.models import AnswerJudgment
    judgment = AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=EvidenceResult.PASS)
    res = submit_answer(
        repo, learner_id="u1", project_id="p1", task=task, interaction=interaction,
        judgment=judgment, answer_text="引用保存对象的地址", policy=ReviewPolicy(),
        submission_id="s1", evidence_id="e1", source_book_id=corp.book_id,
    )
    print(f"written={res.written} verified_levels={[l.value for l in res.verified_levels]}")
    state = repo.get_state("p1", "c_reference")
    print(f"c_reference: current={state.current_verified_level.value} highest={state.highest_ever_level.value}")

    # 3. Next action.
    print("\n--- 3. 下一步行动 (Next Best Action) ---")
    from bookmind.engine.decision.next_action import ConceptView, DecisionInput, decide
    views = []
    for bid in repo.allowed_book_ids("p1"):
        for c in repo.concepts_for_book(bid):
            s = repo.get_state("p1", c.concept_id)
            views.append(ConceptView(concept=c, state=s))
    trace = decide(DecisionInput(
        activity_mode=ActivityMode.READING, intervention_policy=InterventionPolicy.PROACTIVE,
        ui_preset=UIPreset.DEEP_LEARNING, concepts=views,
        misconceptions=repo.all_misconceptions("p1"),
    ))
    print(f"selected: {trace.selected_action} (rule {trace.selected_rule}) — {trace.reason}")

    # 4. Exposure + Learning State (Phase 4).
    print("\n--- 4. 学习状态与曝光 (Learner Model) ---")
    from datetime import timedelta
    from bookmind.domain.enums import EvidenceType
    from bookmind.domain.models import utcnow
    from bookmind.engine.learning_engine import record_exposure, apply_expiry
    from bookmind.services import project_state_views
    now = utcnow()
    # Record reading exposure on a couple of concepts.
    for cid in ("c_variable", "c_polymorphism"):
        r = record_exposure(
            repo, learner_id="u1", project_id="p1", concept_id=cid,
            source_book_id=corp.book_id, evidence_id=f"ex_{cid}",
            evidence_type=EvidenceType.READ, occurred_at=now,
            read_coverage=0.95,
        )
        print(f"  exposure {cid}: {r.exposure_state.value} progress={r.read_progress} changed={r.changed}")

    # Build the Learning State page projection.
    views = project_state_views(repo, "p1", as_of=now, policy=ReviewPolicy())
    groups: dict[str, int] = {}
    for v in views:
        groups[v.group] = groups.get(v.group, 0) + 1
    print(f"  Learning State: {len(views)} concepts — groups={groups}")
    # Show the verified concept with its evidence chain.
    ref = next((v for v in views if v.concept_id == "c_reference"), None)
    if ref:
        print(f"  c_reference: current={ref.current_verified_level} group={ref.group} "
              f"evidence_chain={len(ref.evidence)} L1.effective={ref.levels[0].effective_status}")

    # Demonstrate idempotent expiry on the verified L1.
    far = now + timedelta(days=60)
    exp = apply_expiry(repo, project_id="p1", concept_id="c_reference", level=Level.L1,
                       as_of=far, policy=ReviewPolicy())
    print(f"  expiry c_reference L1 (60d later): expired={exp.expired} recorded={exp.transition_recorded} reason={exp.reason}")
    exp2 = apply_expiry(repo, project_id="p1", concept_id="c_reference", level=Level.L1,
                        as_of=far, policy=ReviewPolicy())
    print(f"  expiry repeat (idempotent): recorded={exp2.transition_recorded} reason={exp2.reason}")

    # 5. Misconception diagnosis closed loop (Phase 5).
    #    Now driven through the *real* Diagnostician (offline fallback) rather
    #    than hand-built judgments — the demo and product paths use the same
    #    service (PRODUCTIZATION §1.3.10).
    print("\n--- 5. 误区诊断闭环 (Misconception Lifecycle) ---")
    from bookmind.agents.bug_library import get_bug
    from bookmind.domain.enums import EvidenceResult as _ER, EvidenceType as _ET, JudgmentStatus as _JS
    from bookmind.domain.models import AnswerJudgment as _AJ, MisconceptionSignal as _MS, TrustedTaskContext as _TTC
    from bookmind.domain.enums import SignalDirection as _SD, SignalStrength as _SS
    from bookmind.engine.misconception.probe_classifier import classify_answer, signals_for_probe
    from bookmind.engine.task.generator import generate_probe, generate_changed_task
    from bookmind.engine.task.validator import validate
    from bookmind.services.remediation import RemediationService
    from bookmind.services.misconception_view import build_trace
    from bookmind.agents.diagnostician import DiagnosticianAgent

    diag = DiagnosticianAgent(router)
    bug = get_bug("bug_ref_vs_object")
    # 5a. Probe classification (offline).
    cls = classify_answer(bug, "a.getValue() returns the original value because b is a separate copy.")
    print(f"  probe classify: best={cls.best_hypothesis} scores={cls.scores}")

    # 5b. Task validation.
    draft = generate_probe(bug, target_concept_ids=["c_reference"])
    vreport = validate(draft, repo, "p1")
    print(f"  probe validation: passed={vreport.passed} stages={[c.name for c in vreport.checks]}")

    # 5c. Drive to CONFIRMED: the Diagnostician judges each wrong answer for
    # real (offline). A non-probe FAIL emits a WEAK FOR signal (one ordinary
    # error does not confirm), so we need three verify FAILs (+1 each) plus one
    # high-discrimination probe FAIL (+3) to reach score 6 and satisfy the
    # "≥2 distinct tasks, ≥1 probe" CONFIRMED gate.
    _wrong = "a.getValue() returns the original value because b is a separate copy."
    for i in range(3):
        _task = _TTC(task_id=f"mis_v{i}", task_version=1, target_concept_ids=["c_reference"],
                     evidence_for_levels=[Level.L2], rubric=bug.rubric)
        _judge = diag.judge(_task, _wrong, rubric=bug.rubric)
        print(f"  verify {i} judge: { _judge.judgment_status.value} {_judge.result} "
              f"signals={[ (s.direction.value, s.strength.value) for s in _judge.misconception_signals]}")
        submit_answer(repo, learner_id="u1", project_id="p1", task=_task, interaction=interaction,
                      judgment=_judge, answer_text=_wrong, policy=ReviewPolicy(),
                      submission_id=f"misv{i}", evidence_id=f"mise{i}", source_book_id=corp.book_id)
    _ptask = _TTC(task_id="mis_probe", task_version=1, target_concept_ids=["c_reference"],
                  evidence_for_levels=[Level.L2], rubric=bug.rubric,
                  is_probe=True, discriminated_bug_ids=[bug.bug_id])
    _pjudge = diag.judge(_ptask, _wrong, rubric=bug.rubric)
    submit_answer(repo, learner_id="u1", project_id="p1", task=_ptask, interaction=interaction,
                  judgment=_pjudge, answer_text=_wrong,
                  policy=ReviewPolicy(), submission_id="misp", evidence_id="misep", source_book_id=corp.book_id)
    mis = repo.get_misconception("p1", bug.bug_id)
    print(f"  after 3 errors: status={mis.status.value} score={mis.evidence_score}")
    assert mis.status.value == "CONFIRMED"

    # 5d. Remediation → REMEDIATING → two changed tasks → RESOLVED.
    #     Correct answers are judged PASS by the real Diagnostician.
    rem_svc = RemediationService(repo, router)
    plan = rem_svc.start(project_id="p1", bug_id=bug.bug_id)
    print(f"  remediation: explanation_goal={plan.explanation_goal[:50]}...")
    _correct = "a.getValue() returns 9 because a and b refer to the same object."
    for stage, fp in [(1, "scene_demo_A"), (2, "scene_demo_B")]:
        cres = rem_svc.build_changed_task(project_id="p1", bug_id=bug.bug_id, stage=stage)
        _cttask = cres.trusted.model_copy(update={"scenario_fingerprint": fp})
        _ctjudge = diag.judge(_cttask, _correct, rubric=bug.rubric)
        submit_answer(repo, learner_id="u1", project_id="p1", task=_cttask, interaction=interaction,
                      judgment=_ctjudge, answer_text=_correct, policy=ReviewPolicy(),
                      submission_id=f"misct{stage}", evidence_id=f"misect{stage}", source_book_id=corp.book_id)
    mis = repo.get_misconception("p1", bug.bug_id)
    print(f"  after 2 changed-task PASS: status={mis.status.value} pass_count={mis.changed_task_pass_count}")
    assert mis.status.value == "RESOLVED"

    # 5e. Relapse on new high-disc probe (real Diagnostician judges the wrong
    # answer FAIL + FOR STRONG).
    _rtask = _TTC(task_id="mis_relapse", task_version=1, target_concept_ids=["c_reference"],
                  evidence_for_levels=[Level.L2], rubric=bug.rubric,
                  is_probe=True, discriminated_bug_ids=[bug.bug_id])
    _rjudge = diag.judge(_rtask, _wrong, rubric=bug.rubric)
    submit_answer(repo, learner_id="u1", project_id="p1", task=_rtask, interaction=interaction,
                  judgment=_rjudge, answer_text=_wrong, policy=ReviewPolicy(),
                  submission_id="misr", evidence_id="miser", source_book_id=corp.book_id)
    mis = repo.get_misconception("p1", bug.bug_id)
    print(f"  after relapse probe: status={mis.status.value}")
    assert mis.status.value == "RELAPSED"

    # 5f. Misconception trace.
    trace = build_trace(repo, "p1", bug.bug_id)
    print(f"  trace: status={trace.status} evidence={len(trace.evidence_chain)} transitions={len(trace.transitions)}")

    # 5g. Gold answer classification test (EVALUATION §3.3).
    from bookmind.evaluation.diagnostician_gold import run_probe_classification
    cls_results = run_probe_classification()
    cls_pass = sum(1 for r in cls_results if r.passed)
    print(f"  gold classification test: {cls_pass}/{len(cls_results)} passed")

    # 6. Long-term recovery (Phase 6, LEARNING_MODEL §12).
    print("\n--- 6. 长期恢复 (Recovery) ---")
    from bookmind.services.recovery import RecoveryService
    from bookmind.evaluation.decision_gold import run_decision_gold
    # The learner has been away 60 days; c_reference L1 is EXPIRED (from step 4).
    rec_svc = RecoveryService(repo)
    rec_plan = rec_svc.build_plan(
        project_id="p1", as_of=far, policy=ReviewPolicy(), last_active_at=now,
    )
    print(f"  days_away={rec_plan.days_away:.0f} candidates={len(rec_plan.candidates)} "
          f"recommendation={rec_plan.recommendation}")
    for c in rec_plan.candidates[:3]:
        print(f"    - {c.concept_id} ({c.reason} R={c.retrievability} goal={c.goal_relevance})")
    # User chooses to continue directly — system respects that (§12.5).
    rec_svc.record_choice(rec_plan, "continue")
    act, tgt = rec_svc.recommend_action(rec_plan)
    print(f"  user chose 'continue' → action={act.value} target={tgt} (no forced check)")
    # Or the user accepts the recovery check → VERIFY the top candidate.
    rec_svc.record_choice(rec_plan, "recovery_check")
    act, tgt = rec_svc.recommend_action(rec_plan)
    print(f"  user chose 'recovery_check' → action={act.value} target={tgt}")
    # Decision gold states (EVALUATION §6.5).
    gold_results = run_decision_gold()
    gold_pass = sum(1 for r in gold_results if r.passed)
    print(f"  decision gold states: {gold_pass}/{len(gold_results)} passed")

    # 7. Benchmark + CI gate (Phase 7).
    print("\n--- 7. Benchmark 与 CI 门禁 (Phase 7) ---")
    from bookmind.evaluation.benchmark import BenchmarkConfig, run_benchmark
    bench = run_benchmark(config=BenchmarkConfig(budget=20, verification_window=10,
                          systems=["bookmind", "b0_basic_tutor", "b2_fixed_flow"]))
    print("  benchmark summary (strict_acc / FMR / coverage):")
    for sys_name, s in bench["summary"].items():
        print(f"    {sys_name:<18} acc={s['strict_accuracy']:.3f} fmr={s['fmr']:.3f} cov={s['coverage']:.3f}")
    from bookmind.evaluation.ci_gate import run_ci_gate
    gate = run_ci_gate()
    gate_pass = sum(1 for g in gate.gates if g.passed)
    print(f"  CI gate: {gate_pass}/{len(gate.gates)} hard gates passed — {'PASS' if gate.all_passed else 'FAIL'}")

    # 8. UI + deployment (Phase 8).
    print("\n--- 8. UI 与部署 (Phase 8) ---")
    print("  frontend: /ui (项目首页 / Reader+Agent / Learning State / Recovery / 误区诊断)")
    print("  start: bash start.sh  →  http://localhost:8765/ui")
    print("  deploy: docker compose up --build  (PostgreSQL + pgvector + backend)")

    print("\n" + "=" * 60)
    print("demo 完成。全部 8 阶段离线可运行。" + ("（本次使用了实时模型）" if live else "（全程离线）"))
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
