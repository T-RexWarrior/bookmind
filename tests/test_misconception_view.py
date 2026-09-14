"""Phase 5 tests: misconception trace view — LEARNING_MODEL §13."""

from __future__ import annotations

from bookmind.domain.enums import MisconceptionStatus, SignalDirection, SignalStrength
from bookmind.domain.models import MisconceptionHypothesis
from bookmind.services.misconception_view import build_trace, project_traces
from bookmind.storage.in_memory import InMemoryRepository


def _repo_with_misconception():
    repo = InMemoryRepository()
    repo.upsert_misconception(MisconceptionHypothesis(
        project_id="p1", bug_id="bug_ref_vs_object", hypothesis_group="ref_group",
        status=MisconceptionStatus.LIKELY, evidence_score=4,
        changed_task_pass_count=1, changed_task_pass_fingerprints=["fp_a"],
        hypothesis_cycle=0,
    ))
    return repo


def test_build_trace_returns_current_status_and_score():
    repo = _repo_with_misconception()
    trace = build_trace(repo, "p1", "bug_ref_vs_object")
    assert trace.bug_id == "bug_ref_vs_object"
    assert trace.status == MisconceptionStatus.LIKELY.value
    assert trace.evidence_score == 4
    assert trace.hypothesis_group == "ref_group"
    assert trace.changed_task_pass_count == 1
    assert trace.changed_task_pass_fingerprints == ["fp_a"]


def test_build_trace_includes_evidence_chain_newest_first():
    repo = _repo_with_misconception()
    from bookmind.domain.enums import EvidenceResult, EvidenceType
    from bookmind.domain.models import Evidence, MisconceptionSignal
    # Two evidences at different times.
    from bookmind.domain.models import utcnow
    from datetime import timedelta
    base = utcnow()
    repo.append_evidence(Evidence(
        evidence_id="e1", event_key="k1", project_id="p1", concept_id="c",
        source_book_id="b", evidence_type=EvidenceType.PROBE, required_level="L2",
        result=EvidenceResult.FAIL, independent=True, task_id="t1",
        occurred_at=base - timedelta(hours=1),
        misconception_signals=[MisconceptionSignal(bug_id="bug_ref_vs_object",
            direction=SignalDirection.FOR, strength=SignalStrength.STRONG)],
        high_discrimination=True,
    ))
    repo.append_evidence(Evidence(
        evidence_id="e2", event_key="k2", project_id="p1", concept_id="c",
        source_book_id="b", evidence_type=EvidenceType.VERIFY, required_level="L2",
        result=EvidenceResult.FAIL, independent=True, task_id="t2",
        occurred_at=base,
        misconception_signals=[MisconceptionSignal(bug_id="bug_ref_vs_object",
            direction=SignalDirection.FOR, strength=SignalStrength.MEDIUM)],
    ))
    trace = build_trace(repo, "p1", "bug_ref_vs_object")
    assert len(trace.evidence_chain) == 2
    # Newest first.
    assert trace.evidence_chain[0].evidence_id == "e2"
    assert trace.evidence_chain[1].evidence_id == "e1"
    # Each item carries its scoring type and direction.
    item = trace.evidence_chain[0]
    assert item.scoring_type  # non-empty
    assert item.direction == "FOR"
    assert item.evidence_type == "VERIFY"


def test_build_trace_includes_transitions():
    repo = _repo_with_misconception()
    import uuid
    from bookmind.domain.models import StateTransition
    repo.record_transition(StateTransition(
        transition_id=str(uuid.uuid4()), entity_type="misconception",
        entity_id="bug_ref_vs_object", project_id="p1",
        old_state="SUSPECTED", new_state="LIKELY", rule_version="rule_v1",
    ))
    trace = build_trace(repo, "p1", "bug_ref_vs_object")
    assert len(trace.transitions) == 1
    assert trace.transitions[0]["old_state"] == "SUSPECTED"
    assert trace.transitions[0]["new_state"] == "LIKELY"


def test_build_trace_unknown_bug_returns_empty_trace():
    repo = _repo_with_misconception()
    trace = build_trace(repo, "p1", "bug_does_not_exist")
    assert trace.bug_id == "bug_does_not_exist"
    assert trace.evidence_score == 0
    assert trace.evidence_chain == []
    assert trace.status == MisconceptionStatus.SUSPECTED.value


def test_build_trace_is_read_only():
    repo = _repo_with_misconception()
    before = repo.get_misconception("p1", "bug_ref_vs_object")
    build_trace(repo, "p1", "bug_ref_vs_object")
    after = repo.get_misconception("p1", "bug_ref_vs_object")
    assert before.status == after.status
    assert before.evidence_score == after.evidence_score


def test_project_traces_builds_all():
    repo = _repo_with_misconception()
    repo.upsert_misconception(MisconceptionHypothesis(
        project_id="p1", bug_id="bug_eq_vs_equals", status=MisconceptionStatus.SUSPECTED,
        evidence_score=2,
    ))
    traces = project_traces(repo, "p1")
    bug_ids = {t.bug_id for t in traces}
    assert bug_ids == {"bug_ref_vs_object", "bug_eq_vs_equals"}
