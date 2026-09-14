"""Graph quality gold set — ROADMAP Phase 3: "图谱质量 gold set".

A set of deterministic, LLM-free assertions every built book graph must
satisfy. Run against the offline demo corpus build; a live-model build must
also pass the same checks (EVALUATION.md L2: human gold-standard invariants).

Each check is a named function returning ``(ok, detail)`` so a runner can
report which invariant failed rather than a bare pass/fail.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import ConceptSource, RelationType
from ..domain.models import Concept, ConceptRelation
from ..engine.book_graph.mapper import is_acyclic
from ..agents.concept_skeleton import skeleton_concept_ids


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def check_acyclic(concepts: list[Concept]) -> CheckResult:
    ok = is_acyclic(concepts)
    return CheckResult("acyclic", ok, "prerequisite graph has a cycle" if not ok else "DAG")


def check_gold_skeleton_preserved(concepts: list[Concept]) -> CheckResult:
    """All 30 gold concept ids must be present and still marked GOLD."""
    by_id = {c.concept_id: c for c in concepts}
    missing = [cid for cid in skeleton_concept_ids() if cid not in by_id]
    if missing:
        return CheckResult("gold_skeleton_preserved", False, f"missing gold ids: {missing}")
    non_gold = [cid for cid in skeleton_concept_ids() if by_id[cid].source != ConceptSource.GOLD.value]
    if non_gold:
        return CheckResult("gold_skeleton_preserved", False, f"gold ids changed source: {non_gold}")
    return CheckResult("gold_skeleton_preserved", True, f"all {len(skeleton_concept_ids())} gold concepts present")


def check_gold_edges_intact(concepts: list[Concept], gold_relations: list[ConceptRelation]) -> CheckResult:
    """Every gold prerequisite edge must survive in the built graph."""
    by_id = {c.concept_id: c for c in concepts}
    gold_edges = {
        (r.source_concept_id, r.target_concept_id)
        for r in gold_relations
        if r.relation == RelationType.PREREQUISITE and r.source == ConceptSource.GOLD.value
    }
    missing = []
    for src, tgt in gold_edges:
        if src in by_id and tgt not in by_id.get(src, Concept(concept_id=src, book_id="", name="")).prerequisites:
            missing.append((src, tgt))
    if missing:
        return CheckResult("gold_edges_intact", False, f"missing gold edges: {missing}")
    return CheckResult("gold_edges_intact", True, f"all {len(gold_edges)} gold edges present")


def check_no_orphan_prerequisites(concepts: list[Concept]) -> CheckResult:
    """Every prerequisite id must refer to a concept that exists in the graph."""
    ids = {c.concept_id for c in concepts}
    orphans: list[tuple[str, str]] = []
    for c in concepts:
        for p in c.prerequisites:
            if p not in ids:
                orphans.append((c.concept_id, p))
    if orphans:
        return CheckResult("no_orphan_prerequisites", False, f"orphans: {orphans[:5]}")
    return CheckResult("no_orphan_prerequisites", True, "all prerequisites resolve")


def check_source_traceable(concepts: list[Concept]) -> CheckResult:
    """Every LLM_PROPOSED concept must carry at least one source_ref back to the
    textbook (LEARNING_MODEL §2 rule 6: "每个节点和关系保留教材来源")."""
    bad = [c.concept_id for c in concepts
           if c.source == ConceptSource.LLM_PROPOSED.value and not c.source_refs]
    if bad:
        return CheckResult("source_traceable", False, f"concepts without source_refs: {bad[:5]}")
    return CheckResult("source_traceable", True, "all LLM concepts have source_refs")


def check_concept_count_in_range(concepts: list[Concept], *, low: int = 30, high: int = 80) -> CheckResult:
    n = len(concepts)
    ok = low <= n <= high
    return CheckResult("concept_count_in_range", ok, f"{n} concepts (target {low}–{high})")


def run_graph_gold_set(
    concepts: list[Concept],
    gold_relations: list[ConceptRelation] | None = None,
    *,
    count_range: tuple[int, int] = (30, 80),
) -> list[CheckResult]:
    """Run the full graph quality gold set. Returns one CheckResult per check."""
    from ..agents.concept_skeleton import PREREQUISITE_EDGES
    if gold_relations is None:
        gold_relations = [
            ConceptRelation(source_concept_id=cid, target_concept_id=p,
                            relation=RelationType.PREREQUISITE, source=ConceptSource.GOLD.value)
            for cid, prereqs in PREREQUISITE_EDGES.items() for p in prereqs
        ]
    low, high = count_range
    return [
        check_acyclic(concepts),
        check_gold_skeleton_preserved(concepts),
        check_gold_edges_intact(concepts, gold_relations),
        check_no_orphan_prerequisites(concepts),
        check_source_traceable(concepts),
        check_concept_count_in_range(concepts, low=low, high=high),
    ]


__all__ = [
    "CheckResult",
    "check_acyclic",
    "check_gold_skeleton_preserved",
    "check_gold_edges_intact",
    "check_no_orphan_prerequisites",
    "check_source_traceable",
    "check_concept_count_in_range",
    "run_graph_gold_set",
]
