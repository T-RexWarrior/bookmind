"""Tests for the deterministic Book Graph Engine (Phase 3).

Covers LEARNING_MODEL §2 build rules:
  - gold skeleton protected (never overwritten, edges kept);
  - duplicate concepts merged by normalised name;
  - prerequisite proposals validated: cycles removed, self-loops dropped;
  - graph stays acyclic;
  - undo diff records only proposal additions, not gold.
"""

from __future__ import annotations

from bookmind.domain.enums import ConceptSource, Difficulty, RelationType
from bookmind.domain.models import Concept, ConceptRelation
from bookmind.domain.proposals import ConceptProposal, RelationProposal, SectionProposal
from bookmind.domain.source_ref import SourceRef
from bookmind.engine.book_graph.mapper import (
    build_book_graph,
    diff_from_build,
    is_acyclic,
    normalise_name,
    validate_prerequisite_edges,
)


def _gold(c_id, name, prereqs=None, chapter="Fundamentals") -> Concept:
    return Concept(
        concept_id=c_id, book_id="b1", name=name, chapter=chapter,
        importance=0.9, difficulty=Difficulty.MEDIUM,
        source=ConceptSource.GOLD.value, prerequisites=prereqs or [],
    )


# --- normalisation --------------------------------------------------------


def test_normalise_name_collapses_whitespace_and_case():
    assert normalise_name("  Polymorphism  ") == "polymorphism"
    assert normalise_name("Equals()。") == "equals()"
    assert normalise_name("Pass  by  Value") == "pass by value"


# --- cycle validation -----------------------------------------------------


def test_validate_edges_keeps_acyclic_drops_cyclic():
    edges = [("a", "b"), ("b", "c"), ("c", "a")]  # c→a closes the cycle
    res = validate_prerequisite_edges(edges)
    assert ("a", "b") in res.kept
    assert ("b", "c") in res.kept
    # c→a would create a cycle a→b→c→a → dropped.
    assert ("c", "a") in [e[:2] for e in res.dropped]
    assert any("cycle" in r for _, _, r in res.dropped)


def test_validate_edges_drops_self_loop():
    res = validate_prerequisite_edges([("a", "a")])
    assert res.kept == []
    assert res.dropped[0][:2] == ("a", "a")
    assert res.dropped[0][2] == "self-loop"


def test_validate_edges_respects_existing_adjacency():
    # Existing graph b→a. Adding a→b would cycle.
    res = validate_prerequisite_edges([("a", "b")], existing_adj={"b": {"a"}})
    assert res.kept == []
    assert any("cycle" in r for _, _, r in res.dropped)


# --- build: gold protection ----------------------------------------------


def test_build_keeps_gold_skeleton_unchanged():
    gold = [_gold("c_var", "Variables", []), _gold("c_ref", "References", ["c_var"])]
    gold_rels = [ConceptRelation(source_concept_id="c_ref", target_concept_id="c_var",
                                 relation=RelationType.PREREQUISITE, source=ConceptSource.GOLD.value)]
    # A proposal that tries to "redefine" Variables with different importance.
    prop = SectionProposal(section_id="s1", section_path=("Fundamentals",), concepts=[
        ConceptProposal(name="Variables", importance=0.1, description="LLM desc"),
    ])
    build = build_book_graph(book_id="b1", gold_concepts=gold, gold_relations=gold_rels,
                             section_proposals=[prop])
    by_id = {c.concept_id: c for c in build.concepts}
    # Gold concept retained with gold importance (not 0.1).
    assert by_id["c_var"].source == ConceptSource.GOLD.value
    assert by_id["c_var"].importance == 0.9
    # Gold edge retained.
    assert ("c_ref", "c_var") in {(r.source_concept_id, r.target_concept_id) for r in build.relations if r.relation == RelationType.PREREQUISITE}
    assert build.gold_protected >= 1


def test_build_does_not_overwrite_gold_edge_with_proposal():
    gold = [_gold("c_var", "Variables", []), _gold("c_ref", "References", ["c_var"])]
    gold_rels = [ConceptRelation(source_concept_id="c_ref", target_concept_id="c_var",
                                 relation=RelationType.PREREQUISITE, source=ConceptSource.GOLD.value)]
    # Proposal: References has NO prerequisites (tries to remove the gold edge).
    prop = SectionProposal(section_id="s1", section_path=("Fundamentals",), concepts=[
        ConceptProposal(name="References", proposed_prerequisites=[]),
    ])
    build = build_book_graph(book_id="b1", gold_concepts=gold, gold_relations=gold_rels,
                             section_proposals=[prop])
    by_id = {c.concept_id: c for c in build.concepts}
    assert "c_var" in by_id["c_ref"].prerequisites  # gold edge survives


# --- build: dedup / merge ------------------------------------------------


def test_build_merges_duplicate_concepts_by_name():
    gold = [_gold("c_var", "Variables", [])]
    prop = SectionProposal(section_id="s1", section_path=("Fundamentals",), concepts=[
        ConceptProposal(name="variables", description="desc A", importance=0.7,
                        source_refs=[SourceRef(document_id="d", physical_page=1)]),
        ConceptProposal(name="Variables", description="desc B", importance=0.8,
                        source_refs=[SourceRef(document_id="d", physical_page=2)]),
    ])
    build = build_book_graph(book_id="b1", gold_concepts=gold, gold_relations=[],
                             section_proposals=[prop])
    # Only one "variables" concept (the gold one), with merged refs.
    names = [normalise_name(c.name) for c in build.concepts]
    assert names.count("variables") == 1
    var = next(c for c in build.concepts if normalise_name(c.name) == "variables")
    assert len(var.source_refs) >= 2


def test_build_creates_new_concept_for_non_gold_proposal():
    gold = [_gold("c_var", "Variables", [])]
    prop = SectionProposal(section_id="s1", section_path=("Generics",), concepts=[
        ConceptProposal(name="Generics", importance=0.7, difficulty=Difficulty.HARD,
                        proposed_prerequisites=["Variables"]),
    ])
    build = build_book_graph(book_id="b1", gold_concepts=gold, gold_relations=[],
                             section_proposals=[prop])
    gen = next(c for c in build.concepts if normalise_name(c.name) == "generics")
    assert gen.source == ConceptSource.LLM_PROPOSED.value
    assert "c_var" in gen.prerequisites  # name resolved to gold id
    assert gen.concept_id in build.added_concept_ids


# --- build: acyclicity ---------------------------------------------------


def test_build_removes_cycle_introduced_by_proposal():
    # Gold: a→b. Proposal: b→a (cycle) and a fresh c→a.
    a = _gold("a", "Concept A", [])
    b = _gold("b", "Concept B", ["a"])
    gold_rels = [ConceptRelation(source_concept_id="b", target_concept_id="a",
                                 relation=RelationType.PREREQUISITE, source=ConceptSource.GOLD.value)]
    prop = SectionProposal(section_id="s1", section_path=("X",), concepts=[
        ConceptProposal(name="Concept A", proposed_prerequisites=["Concept B"]),  # a→b cycle
    ])
    build = build_book_graph(book_id="b1", gold_concepts=[a, b], gold_relations=gold_rels,
                             section_proposals=[prop])
    assert is_acyclic(build.concepts)
    by_id = {c.concept_id: c for c in build.concepts}
    # Gold edge a→b kept (b depends on a). Proposal edge a→b (a depends on b) dropped.
    assert "a" in by_id["b"].prerequisites
    # a should NOT also depend on b (that would be the cyclic proposal edge).
    assert "b" not in by_id["a"].prerequisites


def test_build_result_is_always_acyclic_with_proposals():
    gold = [_gold("c_var", "Variables", []), _gold("c_ref", "References", ["c_var"]),
            _gold("c_obj", "Objects", ["c_ref"])]
    gold_rels = [ConceptRelation(source_concept_id="c_ref", target_concept_id="c_var",
                                 relation=RelationType.PREREQUISITE, source=ConceptSource.GOLD.value),
                 ConceptRelation(source_concept_id="c_obj", target_concept_id="c_ref",
                                 relation=RelationType.PREREQUISITE, source=ConceptSource.GOLD.value)]
    # Proposals with several cross-references, some cyclic.
    prop = SectionProposal(section_id="s1", section_path=("Misc",), concepts=[
        ConceptProposal(name="Variables", proposed_prerequisites=["Objects"]),  # cycle
        ConceptProposal(name="NewC", proposed_prerequisites=["Variables", "References"]),
    ])
    build = build_book_graph(book_id="b1", gold_concepts=gold, gold_relations=gold_rels,
                             section_proposals=[prop])
    assert is_acyclic(build.concepts)


# --- undo diff -----------------------------------------------------------


def test_diff_records_only_proposal_additions():
    gold = [_gold("c_var", "Variables", [])]
    gold_rels: list[ConceptRelation] = []
    prop = SectionProposal(section_id="s1", section_path=("Generics",), concepts=[
        ConceptProposal(name="Generics", proposed_prerequisites=["Variables"]),
    ])
    build = build_book_graph(book_id="b1", gold_concepts=gold, gold_relations=gold_rels,
                             section_proposals=[prop])
    diff = diff_from_build(build)
    # The new concept id is in the diff; gold is not.
    assert len(diff.added_concept_ids) == 1
    assert "c_var" not in diff.added_concept_ids
    # The new edge (generics→c_var) is in the diff; gold edges are not.
    assert len(diff.added_edges) == 1
    gen_id = diff.added_concept_ids[0]
    assert (gen_id, "c_var") in diff.added_edges


def test_undo_diff_excludes_gold_edges():
    gold = [_gold("c_var", "Variables", []), _gold("c_ref", "References", ["c_var"])]
    gold_rels = [ConceptRelation(source_concept_id="c_ref", target_concept_id="c_var",
                                 relation=RelationType.PREREQUISITE, source=ConceptSource.GOLD.value)]
    build = build_book_graph(book_id="b1", gold_concepts=gold, gold_relations=gold_rels,
                             section_proposals=[])
    diff = diff_from_build(build)
    assert diff.added_concept_ids == []
    assert diff.added_edges == []
