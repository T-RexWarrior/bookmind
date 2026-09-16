"""L1 tests: Context Builder — ARCHITECTURE.md §7.

Covers mode-aware inclusion, the Assessment dual-context split (safe backend
vs learner-visible), priority-based trimming, and the hard rule that the
Assessment learner context never contains textbook answers or retrieved chunks.
"""

from __future__ import annotations

from bookmind.domain.enums import ActivityMode, ExposureState, InterventionPolicy, Level
from bookmind.domain.models import LearnerConceptState
from bookmind.services import ContextBuilder, ContextRequest
from bookmind.retrieval.chunk import DocumentChunk
from bookmind.domain.source_ref import SourceRef


def _chunk(cid, content, page=1):
    return DocumentChunk(
        chunk_id=cid, book_id="b1", document_id="d1",
        content=content, source_ref=SourceRef(document_id="d1", chunk_id=cid, physical_page=page),
    )


def _state(cid, current=Level.L1):
    return LearnerConceptState(project_id="p1", concept_id=cid,
                               current_verified_level=current, highest_ever_level=current,
                               exposure_state=ExposureState.SEEN)


def test_standard_context_includes_retrieved_and_state():
    cb = ContextBuilder()
    req = ContextRequest(
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        policy_text="Be concise.", task_text="Explain == vs equals.",
        retrieved_chunks=[_chunk("c1", "equals compares content")],
        learner_states=[_state("c_equals")],
        recent_evidence_summary="L2 PASS on equals",
    )
    ctx = cb.build(req)
    labels = [s.label for s in ctx.segments]
    assert "Retrieved" in labels
    assert "Learner State" in labels
    assert "Recent Evidence" in labels
    assert "Task" in labels
    assert "Policy" in labels
    assert ctx.chunk_ids() == ["c1"]
    # Policy is highest priority → appears first in the render.
    assert ctx.render().index("[Policy]") < ctx.render().index("[Retrieved]")


def test_assessment_learner_context_has_no_answers_or_chunks():
    cb = ContextBuilder()
    req = ContextRequest(
        activity_mode=ActivityMode.ASSESSMENT,
        intervention_policy=InterventionPolicy.QUIET,
        task_text="Q: does == compare content?",
        retrieved_chunks=[_chunk("c1", "the answer is no, == compares references")],
        assessment_answer_text="Answer: false. Rubric: ...",
        learner_states=[_state("c_eq")],
        recent_evidence_summary="old hint",
    )
    ctx = cb.build(req)
    rendered = ctx.render()
    assert "Question" in rendered
    assert "the answer is no" not in rendered  # no retrieved chunks
    assert "Rubric" not in rendered  # no answer/rubric
    assert "old hint" not in rendered  # no old evidence
    assert ctx.chunks == []
    assert "Retrieved" in ctx.omitted_labels
    assert "Answer & Rubric" in ctx.omitted_labels


def test_assessment_safe_backend_contains_answers():
    cb = ContextBuilder()
    req = ContextRequest(
        activity_mode=ActivityMode.ASSESSMENT,
        intervention_policy=InterventionPolicy.QUIET,
        task_text="Q?", retrieved_chunks=[_chunk("c1", "textbook passage")],
        assessment_answer_text="Answer: false. Rubric: ...",
    )
    ctx = cb.build_safe_backend(req)
    assert ctx.is_assessment_safe_backend is True
    rendered = ctx.render()
    assert "textbook passage" in rendered
    assert "Rubric" in rendered


def test_safe_backend_only_for_assessment():
    cb = ContextBuilder()
    req = ContextRequest(
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
    )
    import pytest
    with pytest.raises(ValueError):
        cb.build_safe_backend(req)


def test_trim_drops_lowest_priority_first():
    cb = ContextBuilder()
    # Tiny budget forces trimming.
    req = ContextRequest(
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        policy_text="p", task_text="t",
        retrieved_chunks=[_chunk("c1", "x" * 200)],
        learner_states=[_state("c1")],
        recent_evidence_summary="e" * 200,
        token_budget=20,
    )
    ctx = cb.build(req)
    # Policy and Task (high priority) must survive; Retrieved (lowest) dropped.
    labels = [s.label for s in ctx.segments]
    assert "Policy" in labels
    assert "Retrieved" in ctx.omitted_labels


def test_review_only_carries_filtered_chunks():
    # The builder trusts the service to pre-filter REVIEW candidates; whatever
    # retrieved_chunks arrive are included. This test pins that contract.
    cb = ContextBuilder()
    req = ContextRequest(
        activity_mode=ActivityMode.REVIEW,
        intervention_policy=InterventionPolicy.PROACTIVE,
        retrieved_chunks=[_chunk("c1", "due concept chunk")],
    )
    ctx = cb.build(req)
    assert ctx.chunk_ids() == ["c1"]


def test_model_guidance_excludes_citable_source_but_keeps_learning_metadata():
    ctx = ContextBuilder().build(ContextRequest(
        activity_mode=ActivityMode.READING,
        intervention_policy=InterventionPolicy.PROACTIVE,
        policy_text="Only cite the textbook.",
        retrieved_chunks=[_chunk("c1", "textbook-only fact")],
        learner_states=[_state("c1")],
        conversation_context_text="学习者：那它为什么更快？",
        memory_context_text="学习者标记为已学，仍待验证。",
    ))
    guidance = ctx.render_model_guidance()
    assert "textbook-only fact" not in guidance
    assert "Only cite the textbook" in guidance
    assert "那它为什么更快" in guidance
    assert "标记为已学" in guidance
