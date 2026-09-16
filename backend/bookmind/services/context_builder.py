"""Context Builder — ARCHITECTURE.md §7.

Assembles the evidence pack the model sees each turn. It is *mode-aware*: the
``activity_mode + intervention_policy`` pair decides what is included, and
Assessment uses a dual-context split (a safe backend context with the textbook
+ answers for grading, and a learner-visible context with only the question).

Priority when over budget (no complex dynamic memory)::

    Policy > Task > Current Section > State > Evidence > Retrieved

All retrieval first applies the ``active_project_id + allowed_book_ids`` hard
filter. Default searches the primary book only; reference books join only when
the user explicitly asks. Old conversation lives in the DB and is retrieved on
demand, never the whole history re-sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.enums import ActivityMode, InterventionPolicy
from ..domain.models import LearnerConceptState, MisconceptionHypothesis
from ..retrieval.chunk import DocumentChunk


@dataclass
class ContextSegment:
    """One prioritised slice of the context, with a stable priority rank."""

    priority: int  # lower = higher priority (kept last when trimming)
    label: str
    text: str
    source_chunk_ids: list[str] = field(default_factory=list)


@dataclass
class BuiltContext:
    """The assembled context for one model turn."""

    segments: list[ContextSegment] = field(default_factory=list)
    chunks: list[DocumentChunk] = field(default_factory=list)
    is_assessment_safe_backend: bool = False  # True for the grading-only context
    omitted_labels: list[str] = field(default_factory=list)

    def render(self) -> str:
        """Render the segments in priority order (highest priority first)."""
        ordered = sorted(self.segments, key=lambda s: s.priority)
        return "\n\n".join(f"[{s.label}]\n{s.text}" for s in ordered if s.text)

    def chunk_ids(self) -> list[str]:
        return [c.chunk_id for c in self.chunks]

    def render_model_guidance(self) -> str:
        """Render non-source continuity metadata for a tutor prompt.

        Retrieved chunks are passed separately as citable textbook evidence.
        Keeping them out here makes the trust boundary explicit: dialogue,
        memory and learner state can adapt an explanation, but never support a
        factual textbook claim or a citation.
        """
        return "\n\n".join(
            f"[{segment.label}]\n{segment.text}"
            for segment in sorted(self.segments, key=lambda item: item.priority)
            if segment.text and segment.label != "Retrieved"
        )


# Priority ranks (ARCHITECTURE §7): lower number = higher priority.
P_POLICY = 0
P_TASK = 1
P_CURRENT_SECTION = 2
P_STATE = 3
P_EVIDENCE = 4
P_RETRIEVED = 5


@dataclass
class ContextRequest:
    """Inputs the service layer gathers before building context."""

    activity_mode: ActivityMode
    intervention_policy: InterventionPolicy
    policy_text: str = ""
    task_text: str = ""
    current_section_text: str = ""
    current_section_chunk_ids: list[str] = field(default_factory=list)
    learner_states: list[LearnerConceptState] = field(default_factory=list)
    recent_evidence_summary: str = ""
    conversation_context_text: str = ""
    memory_context_text: str = ""
    retrieved_chunks: list[DocumentChunk] = field(default_factory=list)
    misconceptions: list[MisconceptionHypothesis] = field(default_factory=list)
    # Assessment: the rubric/answer live only in the safe backend context.
    assessment_answer_text: str = ""
    token_budget: int = 4000


class ContextBuilder:
    """Builds mode-aware contexts. Stateless; one instance per project turn."""

    def build(self, req: ContextRequest) -> BuiltContext:
        if req.activity_mode == ActivityMode.ASSESSMENT:
            return self._build_assessment_learner(req)
        return self._build_standard(req)

    # --- standard (Reading / Review) --------------------------------------

    def _build_standard(self, req: ContextRequest) -> BuiltContext:
        segs: list[ContextSegment] = []
        chunks: list[DocumentChunk] = []

        # REVIEW: only include retrieved chunks that are due/weak/prereq/goal —
        # the service layer is responsible for that filtering, so here we just
        # carry them in. For READING we include whatever was retrieved.
        if req.retrieved_chunks:
            chunks.extend(req.retrieved_chunks)
            segs.append(ContextSegment(
                priority=P_RETRIEVED, label="Retrieved",
                text="\n---\n".join(f"{c.short_label()}: {c.content}" for c in req.retrieved_chunks),
                source_chunk_ids=[c.chunk_id for c in req.retrieved_chunks],
            ))

        if req.current_section_text:
            segs.append(ContextSegment(
                priority=P_CURRENT_SECTION, label="Current Section",
                text=req.current_section_text,
                source_chunk_ids=req.current_section_chunk_ids,
            ))
            # The current section's chunks count as context for citation.
            for c in req.retrieved_chunks:
                if c.chunk_id in req.current_section_chunk_ids and c not in chunks:
                    chunks.append(c)

        if req.learner_states:
            segs.append(ContextSegment(
                priority=P_STATE, label="Learner State",
                text=self._render_states(req.learner_states),
            ))

        if req.recent_evidence_summary:
            segs.append(ContextSegment(
                priority=P_EVIDENCE, label="Recent Evidence",
                text=req.recent_evidence_summary,
            ))

        if req.conversation_context_text:
            segs.append(ContextSegment(
                priority=P_TASK, label="Conversation Continuity",
                text=req.conversation_context_text,
            ))

        if req.memory_context_text:
            segs.append(ContextSegment(
                priority=P_STATE, label="Learner Memory",
                text=req.memory_context_text,
            ))

        if req.task_text:
            segs.append(ContextSegment(priority=P_TASK, label="Task", text=req.task_text))

        if req.policy_text:
            segs.append(ContextSegment(priority=P_POLICY, label="Policy", text=req.policy_text))

        return self._trim(segs, chunks, req.token_budget)

    # --- assessment dual context ------------------------------------------

    def build_safe_backend(self, req: ContextRequest) -> BuiltContext:
        """The grading-only context: textbook + answer + rubric. This must
        never be rendered into the learner-visible output or Tutor dialogue
        (PRODUCT_SPEC §8, EVALUATION §2)."""
        if req.activity_mode != ActivityMode.ASSESSMENT:
            raise ValueError("safe backend context is only for ASSESSMENT")
        segs: list[ContextSegment] = []
        chunks: list[DocumentChunk] = list(req.retrieved_chunks)
        if req.retrieved_chunks:
            segs.append(ContextSegment(
                priority=P_RETRIEVED, label="Textbook (backend only)",
                text="\n---\n".join(c.content for c in req.retrieved_chunks),
                source_chunk_ids=[c.chunk_id for c in req.retrieved_chunks],
            ))
        if req.assessment_answer_text:
            segs.append(ContextSegment(
                priority=P_TASK, label="Answer & Rubric (backend only)",
                text=req.assessment_answer_text,
            ))
        ctx = self._trim(segs, chunks, req.token_budget)
        ctx.is_assessment_safe_backend = True
        return ctx

    def _build_assessment_learner(self, req: ContextRequest) -> BuiltContext:
        """The learner-visible Assessment context: only the question, no
        textbook answers, explanations, hints or leak-prone old dialogue."""
        segs: list[ContextSegment] = []
        if req.task_text:
            segs.append(ContextSegment(priority=P_TASK, label="Question", text=req.task_text))
        if req.policy_text:
            segs.append(ContextSegment(priority=P_POLICY, label="Policy", text=req.policy_text))
        # Deliberately NO retrieved chunks, NO state, NO evidence, NO answer.
        ctx = BuiltContext(segments=segs, chunks=[], omitted_labels=[
            "Retrieved", "Current Section", "Learner State", "Recent Evidence", "Answer & Rubric",
        ])
        return ctx

    # --- helpers -----------------------------------------------------------

    def _render_states(self, states: list[LearnerConceptState]) -> str:
        lines = []
        for s in states:
            lines.append(
                f"{s.concept_id}: current={s.current_verified_level.value} "
                f"highest={s.highest_ever_level.value} exposure={s.exposure_state.value}"
            )
        return "\n".join(lines)

    def _trim(self, segs: list[ContextSegment], chunks: list[DocumentChunk], budget: int) -> BuiltContext:
        """Approximate token budget: ~4 chars per token. Trim lowest-priority
        (highest rank number) segments first until under budget."""
        def approx_tokens(text: str) -> int:
            return max(1, len(text) // 4)

        total = sum(approx_tokens(s.text) for s in segs)
        omitted: list[str] = []
        # Trim from the lowest priority (rank 5) upward.
        for rank in sorted({s.priority for s in segs}, reverse=True):
            if total <= budget:
                break
            for s in list(segs):
                if s.priority == rank:
                    total -= approx_tokens(s.text)
                    segs.remove(s)
                    omitted.append(s.label)
        return BuiltContext(segments=segs, chunks=chunks, omitted_labels=omitted)
