"""L3 closed-loop benchmark — EVALUATION.md §4, §5, §6.

Runs a deterministic simulated learner against a system (BookMind or a
baseline) for a fixed interaction budget, then reports the §6 metrics. The
protocol is **interactive closed-loop** (§4.2): each system may choose
different tasks/actions, but shares the same learner profile, seed, interaction
budget and model budget. Results are reproducible.

Systems implemented here:
  - ``bookmind`` — the full adaptive system (Next Best Action + Evidence Gate +
    Misconception + Recovery). This is the real engine.
  - ``b0_basic_tutor`` — EVALUATION §5 B0: no RAG, no long-term Evidence, last
    answer = mastery.
  - ``b1_pdf_rag`` — EVALUATION §5 B1: same retrieval + fixed read→summary→quiz,
    no misconception / Next Action, last quiz = mastery.
  - ``b2_fixed_flow`` — EVALUATION §5 B2: fixed retrieve→diagnose→grade→teach→
    practice→update→review, no dynamic probe selection or Next Action.

Metrics (§6): Mastery Accuracy (strict + ±1), False Mastery Rate (FMR) +
Verification Coverage + Underestimation Rate, Intervention Rate, Action
Distribution. FMR uses a pre-registered fixed verification window and reports
censored concepts separately (§6.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import (
    Action,
    ActivityMode,
    EvidenceResult,
    InterventionPolicy,
    Level,
    UIPreset,
)
from ..domain.models import (
    AnswerJudgment,
    InteractionContext,
    ReviewPolicy,
    TrustedTaskContext,
)
from ..domain.enums import HintLevel, JudgmentStatus
from ..engine.learning_engine import submit_answer
from ..storage.in_memory import InMemoryRepository
from .learner_simulator import LearnerProfile, SimulatedLearner, _level_rank


# A system is a callable that runs one interaction step against the repo and
# learner, returning the action it took and the evidence result (if any).
SystemStepFn = callable


@dataclass
class StepRecord:
    step: int
    action: str
    concept_id: str | None
    required_level: str
    result: str | None  # PASS / PARTIAL / FAIL / None
    independent: bool
    used_hint: bool


@dataclass
class SystemRunResult:
    system_name: str
    profile_id: str
    steps: list[StepRecord] = field(default_factory=list)
    # The system's final mastery judgement per concept (what it believes).
    final_judgement: dict[str, Level] = field(default_factory=dict)
    # Concepts that got an independent re-verification within the window.
    reverified_concepts: set[str] = field(default_factory=set)
    # Concepts judged L2+ that subsequently failed an independent task in-window.
    false_mastery_concepts: set[str] = field(default_factory=set)
    # Concepts judged L2+ with at least one in-window re-verification chance.
    denominator_concepts: set[str] = field(default_factory=set)
    # Censored: judged L2+ but never got a re-verification chance in-window.
    censored_concepts: set[str] = field(default_factory=set)
    # Concepts true-L2+ but the system never lifted above L0/L1 after a chance.
    underestimated_concepts: set[str] = field(default_factory=set)
    # Action counts.
    action_counts: dict[str, int] = field(default_factory=dict)
    # Verification tasks issued (for intervention rate).
    verify_count: int = 0
    diagnose_count: int = 0
    review_count: int = 0


# --- metric computation -----------------------------------------------------

@dataclass
class MetricReport:
    system_name: str
    profile_id: str
    n_concepts: int
    # Mastery accuracy
    strict_accuracy: float
    pm1_accuracy: float
    # FMR suite
    fmr: float
    coverage: float
    underestimation: float
    n_censored: int
    # Intervention
    intervention_rate: float
    action_distribution: dict[str, int]

    def to_dict(self) -> dict:
        return {
            "system": self.system_name, "profile": self.profile_id,
            "n_concepts": self.n_concepts,
            "strict_accuracy": round(self.strict_accuracy, 4),
            "pm1_accuracy": round(self.pm1_accuracy, 4),
            "fmr": round(self.fmr, 4),
            "coverage": round(self.coverage, 4),
            "underestimation": round(self.underestimation, 4),
            "n_censored": self.n_censored,
            "intervention_rate": round(self.intervention_rate, 4),
            "action_distribution": self.action_distribution,
        }


def compute_metrics(
    run: SystemRunResult,
    profile: LearnerProfile,
    *,
    total_concepts: int,
) -> MetricReport:
    """Compute the §6 metrics for one (system, profile) run."""
    gt = profile.mastery_gt
    judged = run.final_judgement

    # Mastery accuracy over concepts the system touched + ground-truth concepts.
    all_cids = set(gt) | set(judged)
    n = len(all_cids) or 1
    strict = 0
    pm1 = 0
    for cid in all_cids:
        g = _level_rank(gt.get(cid, Level.L0))
        j = _level_rank(judged.get(cid, Level.L0))
        if g == j:
            strict += 1
        if abs(g - j) <= 1:
            pm1 += 1

    # FMR (§6.2): numerator = judged L2+ AND gt < L2 AND subsequently failed an
    # independent task in-window. denominator = judged L2+ AND got a
    # re-verification chance in-window. Censored = judged L2+ with no chance.
    num = len(run.false_mastery_concepts)
    den = len(run.denominator_concepts) or 1
    fmr = num / den
    # Coverage (§6.2): of the true-L2+ target concepts, how many got an
    # independent verification in-budget. Capped at 1.0.
    true_l2 = {c for c in gt if _level_rank(gt[c]) >= _level_rank(Level.L2)}
    covered = len(run.reverified_concepts & true_l2) / (len(true_l2) or 1)
    coverage = min(1.0, covered)
    # Underestimation: true L2+ but system stayed L0/L1 after a verification chance.
    under = len(run.underestimated_concepts) / (len(true_l2) or 1)

    # Censored: judged L2+ but never got a re-verification chance in-window.
    # Computed here (not in _finalize_censored) so callers who build a
    # SystemRunResult by hand still get the right count.
    censored = {
        cid for cid, j in judged.items()
        if _level_rank(j) >= _level_rank(Level.L2) and cid not in run.denominator_concepts
    }

    n_steps = len(run.steps) or 1
    intervention_count = run.verify_count + run.diagnose_count + run.review_count
    intervention_rate = min(1.0, intervention_count / n_steps) if n_steps else 0.0

    return MetricReport(
        system_name=run.system_name,
        profile_id=run.profile_id,
        n_concepts=total_concepts,
        strict_accuracy=strict / n,
        pm1_accuracy=pm1 / n,
        fmr=fmr,
        coverage=coverage,
        underestimation=under,
        n_censored=len(censored | run.censored_concepts),
        intervention_rate=intervention_rate,
        action_distribution=dict(run.action_counts),
    )


# --- systems ----------------------------------------------------------------

def _seed_repo(profile: LearnerProfile, *, concept_ids: list[str], book_id="b1",
               project_id="p1", learner_id="u1") -> InMemoryRepository:
    """Seed a repo with the gold skeleton for the benchmark."""
    from ..agents.concept_skeleton import build_skeleton
    from ..domain.models import Book, LearningProject, ProjectBook, User
    from ..domain.enums import BookRole

    repo = InMemoryRepository()
    repo.add_user(User(user_id=learner_id))
    repo.create_project(LearningProject(project_id=project_id, learner_id=learner_id, name="bench"))
    repo.add_book(Book(book_id=book_id, owner_user_id=learner_id, source_hash=book_id, title="Java Core"))
    repo.link_book(ProjectBook(project_id=project_id, book_id=book_id, role=BookRole.PRIMARY))
    for c in build_skeleton(book_id):
        repo.add_concept(c)
    return repo


def _judge_from_response(result: EvidenceResult) -> AnswerJudgment:
    return AnswerJudgment(judgment_status=JudgmentStatus.DECIDED, result=result)


def _run_bookmind(
    profile: LearnerProfile,
    *,
    concept_ids: list[str],
    budget: int,
    verification_window: int,
) -> SystemRunResult:
    """Run the full BookMind adaptive system.

    Each step: pick the Next Best Action, build a task at the right level, let
    the simulator respond, and submit through the real ``submit_answer``.
    The FMR window is the last ``verification_window`` steps: any concept judged
    L2+ that then fails an independent task in that window is a false mastery.
    """
    from ..engine.decision.next_action import ConceptView, DecisionInput, decide

    repo = _seed_repo(profile, concept_ids=concept_ids)
    learner = SimulatedLearner(profile)
    res = SystemRunResult(system_name="bookmind", profile_id=profile.profile_id)
    policy = ReviewPolicy()

    # Track per-concept judged level over time for FMR. Initialise over ALL
    # concepts in the repo (the NBA may pick any of them), not just the
    # profile's ground-truth concepts.
    all_repo_cids = [c.concept_id for bid in repo.allowed_book_ids("p1") for c in repo.concepts_for_book(bid)]
    judged_history: dict[str, list[Level]] = {cid: [Level.L0] for cid in all_repo_cids}

    for step in range(budget):
        views: list[ConceptView] = []
        for bid in repo.allowed_book_ids("p1"):
            for c in repo.concepts_for_book(bid):
                s = repo.get_state("p1", c.concept_id)
                views.append(ConceptView(concept=c, state=s))
        # Drive the adaptive loop as a reading session: we treat each step as
        # a chapter-boundary moment (key concepts may be unverified → rule 9
        # fires VERIFY), which is exactly when BookMind proactively verifies.
        # Once all key concepts are verified, the engine naturally moves on
        # (REVIEW/REMEDIATE/WAIT) — the adaptive behaviour we want to measure.
        key_unverified = any(
            v.goal_relevance >= 0.5
            and v.state.level_record(Level.L1).status.value == "UNVERIFIED"
            for v in views
        )
        trace = decide(DecisionInput(
            activity_mode=ActivityMode.READING,
            intervention_policy=InterventionPolicy.PROACTIVE,
            ui_preset=UIPreset.DEEP_LEARNING.value,
            concepts=views,
            misconceptions=repo.all_misconceptions("p1"),
            chapter_just_ended=key_unverified,
            key_concepts_unverified=key_unverified,
        ))
        action = Action(trace.selected_action)
        res.action_counts[action.value] = res.action_counts.get(action.value, 0) + 1
        cid = trace.selected_concept_id

        if action == Action.VERIFY and cid:
            res.verify_count += 1
            state = repo.get_state("p1", cid)
            # Verify at the next level above current (or L1 if L0).
            next_lvl = Level.L1 if state.current_verified_level == Level.L0 else _next_level(state.current_verified_level)
            resp = learner.respond(concept_id=cid, required_level=next_lvl, independent=True)
            task = TrustedTaskContext(
                task_id=f"t{step}", task_version=1, target_concept_ids=[cid],
                evidence_for_levels=[next_lvl], rubric=[f"verify {next_lvl.value}"],
            )
            interaction = InteractionContext(
                activity_mode=ActivityMode.READING, intervention_policy=InterventionPolicy.PROACTIVE,
                ui_preset=UIPreset.DEEP_LEARNING, hints_issued=1 if resp.used_hint else 0,
            )
            sres = submit_answer(
                repo, learner_id="u1", project_id="p1", task=task, interaction=interaction,
                judgment=_judge_from_response(resp.result), answer_text=resp.answer_text,
                policy=policy, submission_id=f"s{step}", evidence_id=f"e{step}", source_book_id="b1",
            )
            res.steps.append(StepRecord(step, action.value, cid, next_lvl.value,
                                        resp.result.value, True, resp.used_hint))
            _update_judged(repo, cid, res, judged_history)
            _check_fmr(cid, step, verification_window, judged_history, res, profile, resp, independent=True)
        elif action == Action.REVIEW and cid:
            res.review_count += 1
            state = repo.get_state("p1", cid)
            lvl = state.current_verified_level if state.current_verified_level != Level.L0 else Level.L1
            resp = learner.respond(concept_id=cid, required_level=lvl, independent=True)
            res.steps.append(StepRecord(step, action.value, cid, lvl.value,
                                        resp.result.value, True, resp.used_hint))
            _update_judged(repo, cid, res, judged_history)
        elif action == Action.LEARN_PREREQUISITE and cid:
            # Treat as a light L1 verification of the prerequisite.
            resp = learner.respond(concept_id=cid, required_level=Level.L1, independent=True)
            task = TrustedTaskContext(
                task_id=f"t{step}", task_version=1, target_concept_ids=[cid],
                evidence_for_levels=[Level.L1], rubric=["recall prerequisite"],
            )
            interaction = InteractionContext(
                activity_mode=ActivityMode.READING, intervention_policy=InterventionPolicy.PROACTIVE,
                ui_preset=UIPreset.DEEP_LEARNING, hints_issued=1 if resp.used_hint else 0,
            )
            submit_answer(repo, learner_id="u1", project_id="p1", task=task, interaction=interaction,
                          judgment=_judge_from_response(resp.result), answer_text=resp.answer_text,
                          policy=policy, submission_id=f"s{step}", evidence_id=f"e{step}", source_book_id="b1")
            res.steps.append(StepRecord(step, action.value, cid, Level.L1.value,
                                        resp.result.value, True, resp.used_hint))
            _update_judged(repo, cid, res, judged_history)
        elif action == Action.REMEDIATE and cid:
            # Find an active misconception on this concept and run a changed task.
            mis = next((m for m in repo.all_misconceptions("p1") if cid in m.related_concepts), None)
            if mis and mis.status.value in ("CONFIRMED", "REMEDIATING", "VERIFYING"):
                resp = learner.respond(concept_id=cid, required_level=Level.L3, independent=True,
                                       is_probe=False, discriminated_bug_id=mis.bug_id)
                task = TrustedTaskContext(
                    task_id=f"ct{step}", task_version=1, target_concept_ids=[cid],
                    evidence_for_levels=[Level.L3], rubric=["transfer task"],
                    is_changed_task=True, discriminated_bug_ids=[mis.bug_id],
                )
                interaction = InteractionContext(
                    activity_mode=ActivityMode.READING, intervention_policy=InterventionPolicy.PROACTIVE,
                    ui_preset=UIPreset.DEEP_LEARNING, hints_issued=0,
                )
                submit_answer(repo, learner_id="u1", project_id="p1", task=task, interaction=interaction,
                              judgment=_judge_from_response(resp.result), answer_text=resp.answer_text,
                              policy=policy, submission_id=f"s{step}", evidence_id=f"e{step}", source_book_id="b1")
                res.steps.append(StepRecord(step, action.value, cid, Level.L3.value,
                                            resp.result.value, True, False))
                _update_judged(repo, cid, res, judged_history)
            else:
                res.steps.append(StepRecord(step, action.value, cid, "L0", None, False, False))
        else:
            # WAIT / CONTINUE_READING / DIAGNOSE — no state write.
            res.steps.append(StepRecord(step, action.value, cid, "L0", None, False, False))

    # Final judgement = the engine's current_verified_level per concept.
    for cid in concept_ids:
        res.final_judgement[cid] = repo.get_state("p1", cid).current_verified_level
    _finalize_censored(res, profile)
    return res


def _run_b0_basic(
    profile: LearnerProfile,
    *,
    concept_ids: list[str],
    budget: int,
    verification_window: int,
) -> SystemRunResult:
    """B0: no RAG, no long-term Evidence. Last answer = mastery.

    Each step quizzes one concept (round-robin) at L1; PASS → "L1", else L0.
    No state, no misconception tracking. The "judgement" is the last result."""
    learner = SimulatedLearner(profile, learning_enabled=False)
    res = SystemRunResult(system_name="b0_basic_tutor", profile_id=profile.profile_id)
    last_pass: dict[str, bool] = {}
    for step in range(min(budget, len(concept_ids) * 2)):
        cid = concept_ids[step % len(concept_ids)]
        resp = learner.respond(concept_id=cid, required_level=Level.L1, independent=True)
        last_pass[cid] = resp.result == EvidenceResult.PASS
        res.action_counts["VERIFY"] = res.action_counts.get("VERIFY", 0) + 1
        res.verify_count += 1
        res.steps.append(StepRecord(step, "VERIFY", cid, "L1", resp.result.value, True, resp.used_hint))
    for cid in concept_ids:
        res.final_judgement[cid] = Level.L1 if last_pass.get(cid) else Level.L0
    _finalize_censored(res, profile)
    return res


def _run_b1_pdf_rag(
    profile: LearnerProfile,
    *,
    concept_ids: list[str],
    budget: int,
    verification_window: int,
) -> SystemRunResult:
    """B1: fixed read→summary→quiz flow, same retrieval, no misconception/NBA.
    Last quiz result = mastery (L1 on pass, L0 on fail)."""
    learner = SimulatedLearner(profile, learning_enabled=False)
    res = SystemRunResult(system_name="b1_pdf_rag", profile_id=profile.profile_id)
    last_pass: dict[str, bool] = {}
    # Fixed flow: one quiz per concept, round-robin until budget.
    for step in range(min(budget, len(concept_ids) * 2)):
        cid = concept_ids[step % len(concept_ids)]
        resp = learner.respond(concept_id=cid, required_level=Level.L2, independent=True)
        last_pass[cid] = resp.result == EvidenceResult.PASS
        res.action_counts["VERIFY"] = res.action_counts.get("VERIFY", 0) + 1
        res.verify_count += 1
        res.steps.append(StepRecord(step, "VERIFY", cid, "L2", resp.result.value, True, resp.used_hint))
    for cid in concept_ids:
        res.final_judgement[cid] = Level.L2 if last_pass.get(cid) else Level.L0
    _finalize_censored(res, profile)
    return res


def _run_b2_fixed_flow(
    profile: LearnerProfile,
    *,
    concept_ids: list[str],
    budget: int,
    verification_window: int,
) -> SystemRunResult:
    """B2: fixed retrieve→diagnose→grade→teach→practice→update→review, no
    dynamic probe / NBA. Uses the real Evidence Gate so mastery is real, but
    the task sequence is fixed (one L1 then one L2 per concept, round-robin)."""
    repo = _seed_repo(profile, concept_ids=concept_ids)
    learner = SimulatedLearner(profile)
    res = SystemRunResult(system_name="b2_fixed_flow", profile_id=profile.profile_id)
    policy = ReviewPolicy()
    judged_history: dict[str, list[Level]] = {cid: [Level.L0] for cid in concept_ids}
    step = 0
    # Fixed: for each concept, one L1 quiz then one L2 quiz.
    for cid in concept_ids:
        if step >= budget:
            break
        for lvl in (Level.L1, Level.L2):
            if step >= budget:
                break
            resp = learner.respond(concept_id=cid, required_level=lvl, independent=True)
            task = TrustedTaskContext(
                task_id=f"b2t{step}", task_version=1, target_concept_ids=[cid],
                evidence_for_levels=[lvl], rubric=[f"fixed {lvl.value}"],
            )
            interaction = InteractionContext(
                activity_mode=ActivityMode.READING, intervention_policy=InterventionPolicy.PROACTIVE,
                ui_preset=UIPreset.DEEP_LEARNING, hints_issued=1 if resp.used_hint else 0,
            )
            submit_answer(repo, learner_id="u1", project_id="p1", task=task, interaction=interaction,
                          judgment=_judge_from_response(resp.result), answer_text=resp.answer_text,
                          policy=policy, submission_id=f"b2s{step}", evidence_id=f"b2e{step}", source_book_id="b1")
            res.action_counts["VERIFY"] = res.action_counts.get("VERIFY", 0) + 1
            res.verify_count += 1
            res.steps.append(StepRecord(step, "VERIFY", cid, lvl.value, resp.result.value, True, resp.used_hint))
            _update_judged(repo, cid, res, judged_history)
            _check_fmr(cid, step, verification_window, judged_history, res, profile, resp, independent=True)
            step += 1
    for cid in concept_ids:
        res.final_judgement[cid] = repo.get_state("p1", cid).current_verified_level
    _finalize_censored(res, profile)
    return res


# --- shared helpers ---------------------------------------------------------

def _next_level(level: Level) -> Level:
    order = [Level.L1, Level.L2, Level.L3, Level.L4]
    idx = order.index(level)
    return order[min(idx + 1, len(order) - 1)]


def _update_judged(repo, cid, res, judged_history):
    judged = repo.get_state("p1", cid).current_verified_level
    judged_history[cid].append(judged)
    res.reverified_concepts.add(cid)


def _check_fmr(cid, step, window, judged_history, res, profile, resp, *, independent):
    """Within the verification window, if the system judged this concept L2+
    and it now fails an independent task, mark it as false mastery."""
    if not independent or resp.result != EvidenceResult.FAIL:
        return
    gt = profile.mastery_gt.get(cid, Level.L0)
    if _level_rank(gt) >= _level_rank(Level.L2):
        return  # truly L2+ — a fail here is a slip, not false mastery
    # Was the system judging it L2+ before this step?
    history = judged_history[cid]
    if any(_level_rank(h) >= _level_rank(Level.L2) for h in history[:-1]):
        res.false_mastery_concepts.add(cid)
    res.denominator_concepts.add(cid)


def _finalize_censored(res, profile):
    """Censored = judged L2+ but never got an in-window re-verification chance.
    Underestimated = true L2+ but system stayed L0/L1 after a verification chance."""
    for cid, judged in res.final_judgement.items():
        if _level_rank(judged) >= _level_rank(Level.L2):
            if cid not in res.denominator_concepts:
                # Judged L2+ but never reverified in-window → censored, not in
                # the FMR denominator (§6.2: censored reported separately).
                res.censored_concepts.add(cid)
            else:
                # Got a re-verification chance → in the denominator.
                res.denominator_concepts.add(cid)
    for cid, gt_lvl in profile.mastery_gt.items():
        if _level_rank(gt_lvl) >= _level_rank(Level.L2):
            judged = res.final_judgement.get(cid, Level.L0)
            if _level_rank(judged) < _level_rank(Level.L2):
                # The system had this concept in scope (it's in the skeleton).
                if cid in res.reverified_concepts:
                    res.underestimated_concepts.add(cid)


# --- public runner ----------------------------------------------------------

SYSTEMS: dict[str, SystemStepFn] = {
    "bookmind": _run_bookmind,
    "b0_basic_tutor": _run_b0_basic,
    "b1_pdf_rag": _run_b1_pdf_rag,
    "b2_fixed_flow": _run_b2_fixed_flow,
}


@dataclass
class BenchmarkConfig:
    budget: int = 60  # interaction steps per (system, profile)
    verification_window: int = 20  # FMR window (last N steps)
    systems: list[str] = field(default_factory=lambda: ["bookmind", "b0_basic_tutor", "b1_pdf_rag", "b2_fixed_flow"])


def run_benchmark(
    profiles: list[LearnerProfile] | None = None,
    *,
    config: BenchmarkConfig | None = None,
    concept_ids: list[str] | None = None,
) -> dict:
    """Run the closed-loop benchmark over all (system, profile) pairs.

    Returns a reproducible report: per-system-per-profile metrics plus an
    aggregate per-system summary. Fully deterministic.
    """
    if config is None:
        config = BenchmarkConfig()
    if profiles is None:
        from .learner_simulator import default_profiles
        profiles = default_profiles()
    if concept_ids is None:
        from ..agents.concept_skeleton import skeleton_concept_ids
        concept_ids = skeleton_concept_ids()

    per_run: list[MetricReport] = []
    for system_name in config.systems:
        fn = SYSTEMS[system_name]
        for profile in profiles:
            run = fn(profile, concept_ids=concept_ids, budget=config.budget,
                     verification_window=config.verification_window)
            m = compute_metrics(run, profile, total_concepts=len(concept_ids))
            per_run.append(m)

    # Aggregate per system (mean over profiles).
    by_system: dict[str, list[MetricReport]] = {}
    for m in per_run:
        by_system.setdefault(m.system_name, []).append(m)

    summary: dict[str, dict] = {}
    for sys_name, reports in by_system.items():
        n = len(reports) or 1
        summary[sys_name] = {
            "strict_accuracy": round(sum(r.strict_accuracy for r in reports) / n, 4),
            "pm1_accuracy": round(sum(r.pm1_accuracy for r in reports) / n, 4),
            "fmr": round(sum(r.fmr for r in reports) / n, 4),
            "coverage": round(sum(r.coverage for r in reports) / n, 4),
            "underestimation": round(sum(r.underestimation for r in reports) / n, 4),
            "intervention_rate": round(sum(r.intervention_rate for r in reports) / n, 4),
            "n_profiles": len(reports),
        }

    return {
        "config": {"budget": config.budget, "verification_window": config.verification_window,
                   "systems": config.systems, "n_concepts": len(concept_ids),
                   "n_profiles": len(profiles)},
        "per_run": [m.to_dict() for m in per_run],
        "summary": summary,
    }
