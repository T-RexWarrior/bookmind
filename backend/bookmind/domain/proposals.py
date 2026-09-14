"""Book Mapper proposals — ARCHITECTURE.md §3.1, LEARNING_MODEL.md §2.

These are the *outputs* of the Book Mapper Agent, not stored domain entities.
They are transient: the deterministic Book Graph Engine validates, dedups,
merges and resolves them into stored :class:`~bookmind.domain.models.Concept`
and :class:`~bookmind.domain.models.ConceptRelation` records.

Key contract (LEARNING_MODEL §2 rules 3 & 5):
  - prerequisites here are *proposals only*; the Engine decides which survive
    (cycle removal, cross-chapter check, gold-edge protection);
  - the human gold skeleton is never overwritten by these proposals.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..domain.enums import Difficulty, RelationType
from ..domain.source_ref import SourceRef


class ConceptProposal(BaseModel):
    """A draft concept extracted from one section."""

    name: str
    description: str = ""
    chapter: str = ""
    section: str = ""
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    difficulty: Difficulty = Difficulty.MEDIUM
    source_refs: list[SourceRef] = Field(default_factory=list)
    rationale: str = ""
    # Proposed prerequisite / related concept *names* (not ids). Names are
    # resolved to concept ids during aggregation, after dedup.
    proposed_prerequisites: list[str] = Field(default_factory=list)
    proposed_related: list[str] = Field(default_factory=list)


class RelationProposal(BaseModel):
    """A proposed edge between two concepts, by name."""

    source_name: str  # the concept that depends on / relates to the target
    target_name: str  # the prerequisite / related concept
    relation: RelationType = RelationType.PREREQUISITE
    rationale: str = ""
    source_refs: list[SourceRef] = Field(default_factory=list)


class SectionProposal(BaseModel):
    """Everything the Book Mapper proposes for one section."""

    section_id: str
    section_path: tuple[str, ...] = ()
    concepts: list[ConceptProposal] = Field(default_factory=list)
    relations: list[RelationProposal] = Field(default_factory=list)
    # ``fallback`` is True when the offline deterministic path produced this
    # (no live model). The Engine records provenance either way.
    fallback: bool = False


__all__ = ["ConceptProposal", "RelationProposal", "SectionProposal"]
