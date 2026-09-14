"""Learning Engine facade — the single state-write entry point.

Implements the ``submit_answer`` transaction from ARCHITECTURE.md §10:

  1. freeze TrustedTaskContext + InteractionContext + answer_hash + task_version
  2. (LLM produces AnswerJudgment *outside* this transaction — passed in)
  3. validate schema, target-concept scope, rubric; NEEDS_REVIEW exits early
  4. derive required_level / hint_level / independent from trusted contexts
  5. synthesize idempotent Evidence (event_key dedup)
  6. Engine replays effective Evidence → recompute mastery + misconception
  7. record state_transitions
  8. return a structured result

Scope rules enforced (PRODUCT_SPEC §8, EVALUATION.md §2):
  - evidence concept & source_book must be within the project's allowed books
  - cross-project / cross-user evidence is rejected
  - the learner is taken from the trusted session, never the request body
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..domain.enums import (
    AgentName,
    EvidenceResult,
    EvidenceType,
    HintLevel,
    JudgmentStatus,
    Level,
    LevelStatus,
    MisconceptionStatus,
)
from ..domain.models import (
    AnswerJudgment,
    Evidence,
    InteractionContext,
    MisconceptionHypothesis,
    MisconceptionSignal,
    StateTransition,
    TrustedTaskContext,
)
from ..engine.evidence.gate import can_verify_mastery
from ..engine.exposure.state import ExposureEvent, apply_exposure, is_exposure_only
from ..engine.mastery.state import recompute
from ..engine.misconception.scoring import score_for
from ..engine.misconception.state_machine import update as mis_update
from ..engine.review.forgetting import (
    initial_stability,
    reschedule_after_fail,
    reschedule_after_partial,
    review_due_after_pass,
    stability_after_independent_pass,
)
from ..domain.models import ReviewPolicy
from ..storage.protocols import Repository, ScopeError


RULE_VERSION = "rule_v1"


@dataclass
class SubmitResult:
    written: bool  # False iff event_key was a replay
    evidence_id: str | None
    evidence: Evidence | None
    verified_levels: list[Level] = field(default_factory=list)
    mastery_transitions: list[StateTransition] = field(default_factory=list)
    misconception_transitions: list[StateTransition] = field(default_factory=list)
    gate_blocks: list[str] = field(default_factory=list)
    needs_review: bool = False
    reason: str = ""


def _event_key(project_id: str, task_id: str, task_version: int, submission_id: str) -> str:
    raw = f"{project_id}|{task_id}|{task_version}|{submission_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _derive_independent(interaction: InteractionContext) -> bool:
    """Independent iff no hints were issued and no answer-revealing tools exposed."""
    if interaction.hints_issued > 0:
        return False
    leaky = {"textbook_answer", "solution_reveal", "hint"}
    return not any(t in leaky for t in interaction.tools_exposed)


def _derive_hint_level(interaction: InteractionContext) -> HintLevel:
    if interaction.hints_issued <= 0:
        return HintLevel.NONE
    if interaction.hints_issued == 1:
        return HintLevel.LOW
    if interaction.hints_issued == 2:
        return HintLevel.MEDIUM
    return HintLevel.HIGH


def _derive_required_level(task: TrustedTaskContext) -> Level:
    # The required level is the highest level the task declares evidence for.
    order = {Level.L0: 0, Level.L1: 1, Level.L2: 2, Level.L3: 3, Level.L4: 4}
    if not task.evidence_for_levels:
        return Level.L0
    return max(task.evidence_for_levels, key=lambda l: order[l])


def submit_answer(
    repo: Repository,
    *,
    learner_id: str,
    project_id: str,
    task: TrustedTaskContext,
    interaction: InteractionContext,
    judgment: AnswerJudgment,
    answer_text: str,
    policy: ReviewPolicy,
    submission_id: str,
    evidence_id: str,
    source_book_id: str,
) -> SubmitResult:
    """Run the full state-write transaction for one submitted answer."""

    # 0. Scope: project must belong to this learner.
    repo.assert_project_owned_by(project_id, learner_id)

    # 1. NEEDS_REVIEW → no state increment.
    if judgment.judgment_status == JudgmentStatus.NEEDS_REVIEW:
        return SubmitResult(
            written=False,
            evidence_id=None,
            evidence=None,
            needs_review=True,
            reason="judgment NEEDS_REVIEW; no mastery/misconception increment",
        )

    # 2. Scope: every target concept must be within this project's books.
    allowed_books = repo.allowed_book_ids(project_id)
    if source_book_id not in allowed_books:
        raise ScopeError(f"source_book {source_book_id} not in project {project_id}'s allowed books")
    for cid in task.target_concept_ids:
        if not repo.concept_in_project_scope(cid, project_id):
            raise ScopeError(f"concept {cid} not in project {project_id} scope")

    # 3. Idempotency key.
    ekey = _event_key(project_id, task.task_id, task.task_version, submission_id)

    # 4. Derive trusted fields the Diagnostician must not set.
    independent = _derive_independent(interaction)
    hint_level = _derive_hint_level(interaction)
    required_level = _derive_required_level(task)

    # 4b. Probe classification (LEARNING_MODEL §8/§9). A diagnostic probe must
    # push exactly one competing hypothesis toward CONFIRMED and write AGAINST
    # for the others. When the caller/Diagnostician already supplied explicit
    # FOR/AGAINST signals we trust them; otherwise we classify the answer text
    # deterministically so the invariant holds even offline.
    signals = list(judgment.misconception_signals)
    if task.is_probe and task.discriminated_bug_ids and not signals:
        from .misconception.probe_classifier import signals_for_probe
        from ..agents.bug_library import BUG_LIBRARY
        bid = task.discriminated_bug_ids[0]
        bug = BUG_LIBRARY.get(bid)
        if bug is not None:
            signals = signals_for_probe(bug, answer_text)

    # 5. Synthesize Evidence (append-only).
    result = judgment.result
    evidence = Evidence(
        evidence_id=evidence_id,
        event_key=ekey,
        project_id=project_id,
        concept_id=task.target_concept_ids[0],
        source_book_id=source_book_id,
        evidence_type=(EvidenceType.PROBE if task.is_probe else (EvidenceType.CHANGED_TASK if task.is_changed_task else EvidenceType.VERIFY)),
        required_level=required_level,
        result=result,
        independent=independent,
        hint_level=hint_level,
        task_id=task.task_id,
        task_version=task.task_version,
        misconception_signals=signals,
        discriminated_bug_ids=task.discriminated_bug_ids,
        scenario_fingerprint=task.scenario_fingerprint,
        high_discrimination=task.is_probe,
        content_summary=answer_text[:200],
    )

    # 5–8 run inside one transaction (P0-05): evidence, mastery state,
    # misconception state and transitions commit atomically. A failure in any
    # later step rolls back the evidence write too, so a retry with the same
    # event_key is NOT poisoned by a half-committed state. On the SQL repo this
    # binds a single Session; on the in-memory repo it is a no-op (dict writes
    # are already atomic within the thread).
    with repo.transaction():
        written = repo.append_evidence(evidence)
        if not written:
            # Replay: event_key already exists. Do not re-write or re-update state.
            return SubmitResult(
                written=False,
                evidence_id=evidence.evidence_id,
                evidence=evidence,
                reason="event_key replay; idempotent no-op",
            )

        # 6. Mastery update via Evidence Gate.
        as_of = evidence.occurred_at
        state = repo.get_state(project_id, evidence.concept_id)
        old_level = state.current_verified_level

        gate = can_verify_mastery(evidence, task, judgment)
        if gate.passed_gate:
            _apply_pass(state, gate.verified_levels, as_of, policy)
        elif evidence.result == EvidenceResult.FAIL and independent and not task.is_changed_task and not task.is_probe:
            _apply_independent_fail(state, as_of, policy)
        elif evidence.result == EvidenceResult.PARTIAL:
            _apply_partial(state, as_of, policy)
        # CHANGED_TASK / PROBE mastery effects handled via misconception flow below
        # and the gate (a changed-task PASS that declares evidence_for_levels can
        # also verify mastery through the gate).

        state = recompute(state, as_of, policy)
        state.bump_version()
        repo.save_state(state)

        mastery_transitions: list[StateTransition] = []
        if state.current_verified_level != old_level:
            t = _transition("mastery", evidence.concept_id, project_id, old_level.value, state.current_verified_level.value, evidence.evidence_id)
            mastery_transitions.append(t)
            repo.record_transition(t)

        # 7. Misconception update for each distinct bug_id in signals.
        mis_transitions: list[StateTransition] = []
        bug_ids = {s.bug_id for s in signals}
        if task.is_changed_task and task.discriminated_bug_ids:
            bug_ids |= set(task.discriminated_bug_ids)
        for bug_id in bug_ids:
            mis_evidence = repo.evidence_for_misconception(project_id, bug_id)
            if not mis_evidence:
                continue
            existing = repo.get_misconception(project_id, bug_id)
            if existing is None:
                # Seed related_concepts from the BugLibrary so the decision engine
                # can match the bug to its concepts (rules 2-5 select DIAGNOSE/
                # REMEDIATE/VERIFY by `concept_id in mis.related_concepts`).
                from ..agents.bug_library import BUG_LIBRARY
                bug = BUG_LIBRARY.get(bug_id)
                related = list(bug.related_concepts) if bug is not None else []
                existing = MisconceptionHypothesis(
                    project_id=project_id, bug_id=bug_id, status="SUSPECTED",
                    related_concepts=related,
                )
            new_evidence_list = [e for e in mis_evidence]
            res = mis_update(existing, new_evidence_list, new_evidence=[evidence])
            repo.upsert_misconception(res.hypothesis)
            if res.changed:
                t = _transition("misconception", bug_id, project_id, res.old_status.value, res.new_status.value, evidence.evidence_id)
                mis_transitions.append(t)
                repo.record_transition(t)

        # 8. Mutual-exclusion (LEARNING_MODEL §8): at most one CONFIRMED hypothesis
        # per hypothesis_group. The probe classifier already biases one probe toward
        # a single hypothesis; this is the invariant backstop for multi-task accumulation.
        if bug_ids:
            from .misconception.mutual_exclusion import enforce_for_bugs
            mutex_transitions = enforce_for_bugs(repo, project_id, list(bug_ids))
            mis_transitions.extend(mutex_transitions)

        return SubmitResult(
            written=True,
            evidence_id=evidence.evidence_id,
            evidence=evidence,
            verified_levels=gate.verified_levels,
            mastery_transitions=mastery_transitions,
            misconception_transitions=mis_transitions,
            gate_blocks=gate.blocks,
            reason="ok",
        )


def _transition(entity_type, entity_id, project_id, old, new, eid) -> StateTransition:
    import uuid
    return StateTransition(
        transition_id=str(uuid.uuid4()),
        entity_type=entity_type,
        entity_id=entity_id,
        project_id=project_id,
        old_state=old,
        new_state=new,
        triggering_evidence_id=eid,
        rule_version=RULE_VERSION,
    )


def start_remediation(
    repo: Repository,
    *,
    project_id: str,
    bug_id: str,
) -> MisconceptionHypothesis | None:
    """Flip a CONFIRMED (or RELAPSED-without-competing) hypothesis to REMEDIATING.

    Called by the service layer when the decision engine selects REMEDIATE and
    the Tutor renders the correction content. This is the explicit
    "CONFIRMED → 开始纠正 → REMEDIATING" transition.
    """
    existing = repo.get_misconception(project_id, bug_id)
    if existing is None:
        return None
    if existing.status != MisconceptionStatus.CONFIRMED:
        return existing
    res = mis_update(existing, [e for e in repo.evidence_for_misconception(project_id, bug_id)], remediation_started=True)
    repo.upsert_misconception(res.hypothesis)
    return res.hypothesis


# --- exposure (LEARNING_MODEL §3) ----------------------------------------

def record_exposure(
    repo: Repository,
    *,
    learner_id: str,
    project_id: str,
    concept_id: str,
    source_book_id: str,
    evidence_id: str,
    evidence_type: EvidenceType,
    occurred_at,
    read_coverage: float | None = None,
    explicit_complete: bool = False,
    read_coverage_threshold: float = 0.9,
    source_chunk_ids: list[str] | None = None,
    source_session: str = "",
    content_summary: str = "",
) -> "ExposureResult":
    """Write an exposure-only Evidence (READ/QUESTION/EXPLANATION) and advance
    the concept's exposure state. Exposure never passes the Evidence Gate, so
    it can never upgrade mastery (LEARNING_MODEL §3, §4).
    """
    from .exposure.state import ExposureResult  # local to avoid cycle in dataclass

    if not is_exposure_only(evidence_type.value):
        raise ValueError(f"{evidence_type} is not an exposure-only evidence type")

    repo.assert_project_owned_by(project_id, learner_id)
    allowed = repo.allowed_book_ids(project_id)
    if source_book_id not in allowed:
        raise ScopeError(f"source_book {source_book_id} not in project {project_id}'s allowed books")
    if not repo.concept_in_project_scope(concept_id, project_id):
        raise ScopeError(f"concept {concept_id} not in project {project_id} scope")

    event = ExposureEvent(
        kind="completed" if explicit_complete else ("read_progress" if read_coverage is not None else "seen"),
        coverage=read_coverage,
        explicit=explicit_complete,
    )

    ekey = _event_key(project_id, f"exposure|{concept_id}|{evidence_type.value}", 1, evidence_id)
    evidence = Evidence(
        evidence_id=evidence_id,
        event_key=ekey,
        project_id=project_id,
        concept_id=concept_id,
        source_book_id=source_book_id,
        source_chunk_ids=source_chunk_ids or [],
        evidence_type=evidence_type,
        required_level=Level.L0,
        result=None,
        independent=False,
        hint_level=HintLevel.NONE,
        task_id=f"exposure|{concept_id}",
        task_version=1,
        occurred_at=occurred_at,
        source_session=source_session,
        content_summary=content_summary or f"exposure:{evidence_type.value}",
    )
    written = repo.append_evidence(evidence)

    state = repo.get_state(project_id, concept_id)
    res = apply_exposure(state, event, read_coverage_threshold=read_coverage_threshold)
    if res.changed:
        new_state = state.model_copy(update={
            "exposure_state": res.exposure_state,
            "read_progress": res.read_progress,
        })
        new_state.bump_version()
        repo.save_state(new_state)
        repo.record_transition(_transition(
            "exposure", concept_id, project_id,
            state.exposure_state.value, new_state.exposure_state.value,
            evidence.evidence_id if written else None,
        ))
    return ExposureResult(
        exposure_state=res.exposure_state,
        read_progress=res.read_progress,
        changed=res.changed and written,
        reason=("ok" if written else "event_key replay; exposure idempotent"),
    )


# --- per-level application helpers ----------------------------------------

def _apply_pass(state, verified_levels: list[Level], as_of, policy: ReviewPolicy) -> None:
    for lvl in verified_levels:
        rec = state.level_record(lvl)
        s_new = stability_after_independent_pass(rec.stability_days or initial_stability(lvl, policy), rec.verified_at, as_of, policy)
        due = review_due_after_pass(as_of, s_new, as_of, policy)
        state.set_level_record(lvl, rec.model_copy(update={
            "status": LevelStatus.VERIFIED,
            "verified_at": as_of,
            "stability_days": s_new,
            "review_due_at": due,
        }))


def _apply_independent_fail(state, as_of, policy: ReviewPolicy) -> None:
    """First independent FAIL at current level → UNSTABLE; schedule changed task."""
    lvl = state.current_verified_level
    if lvl == Level.L0:
        return  # nothing to destabilise
    rec = state.level_record(lvl)
    due = reschedule_after_fail(rec.stability_days, as_of, policy)
    state.set_level_record(lvl, rec.model_copy(update={
        "status": LevelStatus.UNSTABLE,
        "review_due_at": due,
    }))


def _apply_partial(state, as_of, policy: ReviewPolicy) -> None:
    """PARTIAL or hinted PASS: keep evidence, reschedule ~1 day later, no upgrade."""
    lvl = state.current_verified_level
    if lvl == Level.L0:
        return
    rec = state.level_record(lvl)
    due = reschedule_after_partial(rec.verified_at, as_of, policy)
    state.set_level_record(lvl, rec.model_copy(update={"review_due_at": due}))


# --- expiry persistence (LEARNING_MODEL §6) --------------------------------

def expiry_event_key(concept_id: str, level: Level, due_at, policy_version: int) -> str:
    """The idempotent key for a derived expiry transition.

    LEARNING_MODEL §6: "在需要持久化转移时使用
    ``expiry:{concept_id}:{level}:{due_at}:{review_policy_version}`` 作为幂等事件键；
    不得因为重复读取反复生成状态事件".

    Note this is *not* the same shape as the submit_answer event_key — expiry is
    a derived, read-time event, so its key is built from the level + the due_at
    it refers to + the policy version. Re-reading at the same as_of reproduces
    the same key and is therefore a no-op.
    """
    import hashlib
    due_str = due_at.isoformat() if due_at is not None else "none"
    raw = f"expiry|{concept_id}|{level.value}|{due_str}|{policy_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class ExpiryResult:
    """Outcome of an expiry check for one (concept, level) pair."""
    concept_id: str
    level: Level
    expired: bool
    transition_recorded: bool  # True iff a new expiry transition was persisted
    reason: str = ""


def apply_expiry(
    repo: Repository,
    *,
    project_id: str,
    concept_id: str,
    level: Level,
    as_of,
    policy: ReviewPolicy,
) -> ExpiryResult:
    """Check one level for expiry and, if it newly transitioned VERIFIED→EXPIRED,
    persist the derived transition with an idempotent ``expiry:...`` key.

    This is the *persist* path for derived expiry. Reading state via
    ``recompute`` always derives EXPIRED on the fly; this function is called
    only when the system decides to durably record the transition (e.g. when
    scheduling a review). Re-calling with the same ``as_of`` is a no-op because
    the expiry event_key is deterministic in ``(concept, level, due_at, policy)``.
    """
    state = repo.get_state(project_id, concept_id)
    rec = state.level_record(level)

    # Idempotency first: if this exact expiry (concept, level, due_at, policy)
    # was already persisted, never record a duplicate — even if the raw status
    # has since been overwritten to EXPIRED by the first call (LEARNING_MODEL §6:
    # "不得因为重复读取反复生成状态事件").
    ekey = expiry_event_key(concept_id, level, rec.review_due_at, policy.review_policy_version)
    if ekey in repo.expiry_keys():
        return ExpiryResult(concept_id, level, expired=True, transition_recorded=False,
                            reason="expiry already recorded (idempotent)")

    if rec.status != LevelStatus.VERIFIED:
        return ExpiryResult(concept_id, level, expired=rec.status == LevelStatus.EXPIRED,
                            transition_recorded=False, reason=f"status={rec.status.value}; not VERIFIED")
    from ..engine.review.forgetting import is_expired
    if not is_expired(rec.verified_at, rec.stability_days, as_of, policy):
        return ExpiryResult(concept_id, level, expired=False, transition_recorded=False,
                            reason="still above threshold")

    # Newly expired — persist the derived transition idempotently.
    repo.record_expiry_key(ekey)

    new_rec = rec.model_copy(update={"status": LevelStatus.EXPIRED})
    new_state = state.model_copy()
    new_state.set_level_record(level, new_rec)
    new_state = recompute(new_state, as_of, policy)
    new_state.bump_version()
    repo.save_state(new_state)

    t = _transition("review", concept_id, project_id,
                    LevelStatus.VERIFIED.value, LevelStatus.EXPIRED.value, None)
    t.rule_version = f"expiry_policy_v{policy.review_policy_version}"
    repo.record_transition(t)
    return ExpiryResult(concept_id, level, expired=True, transition_recorded=True,
                        reason="VERIFIED→EXPIRED recorded")
