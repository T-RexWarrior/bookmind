"""Book Mapping service — orchestrates the Book Mapping use case (Phase 3).

Wires the Book Mapper Agent over every section of a book → the deterministic
Book Graph Engine → the repository. This is the service the HTTP API calls for
``/map-book`` and ``/undo-mapping``. It owns scope checks and the gold-skeleton
protection contract; Agents and the Engine never touch storage directly.

Map–Aggregate flow (ARCHITECTURE §4.1):
  1. gather the book's parsed sections + their chunks;
  2. for each section, ask the Book Mapper for proposals (passing the known
     concept names so the model reuses them);
  3. feed all section proposals + the gold skeleton into the Book Graph Engine,
     which dedups, merges, removes cycles and protects gold edges;
  4. persist the resulting concepts + relations into the book's scope;
  5. return a build report (counts, dropped edges, diff for undo).

The undo path removes only the *proposal-origin* additions (concepts and edges
added by the last mapping), never gold skeleton concepts/edges or any learner
state (LEARNING_MODEL §2: "错误候选可撤销而不影响其他学习数据").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agents.book_mapper import BookMapperAgent
from ..agents.concept_skeleton import PREREQUISITE_EDGES, build_skeleton, skeleton_concept_ids
from ..agents.demo_corpus import DEMO_BOOK_ID
from ..domain.enums import ConceptSource, RelationType
from ..domain.models import Concept, ConceptRelation
from ..engine.book_graph.mapper import BookGraphBuild, build_book_graph, diff_from_build
from ..llm.router import ModelRouter
from ..retrieval.chunk import DocumentChunk
from ..retrieval.parsed_document import ParsedDocument, Section
from ..storage.protocols import Repository, ScopeError
from .concept_scope import is_learning_section


@dataclass
class MappingReport:
    book_id: str
    total_concepts: int
    gold_concepts: int
    new_concepts: int
    total_prereq_edges: int
    dropped_edges: list[tuple[str, str, str]] = field(default_factory=list)
    gold_protected: int = 0
    fallback_sections: int = 0
    # Reversible diff of proposal additions.
    added_concept_ids: list[str] = field(default_factory=list)
    added_edges: list[tuple[str, str]] = field(default_factory=list)
    reused: bool = False  # True if a cached mapping was reused (same inputs)


class BookMappingService:
    """Orchestrates Book Mapping for one book within a project's scope."""

    def __init__(self, repo: Repository, router: ModelRouter) -> None:
        self.repo = repo
        self.mapper = BookMapperAgent(router)
        # Cache: (book_id, graph_key) → MappingReport, for idempotent re-runs.
        self._cache: dict[tuple[str, str], MappingReport] = {}

    # --- main entry -------------------------------------------------------

    def map_book(
        self,
        *,
        project_id: str,
        learner_id: str,
        book_id: str,
        parsed_document: ParsedDocument | None = None,
        chunks: list[DocumentChunk] | None = None,
        graph_key: str = "",
    ) -> MappingReport:
        """Build (or rebuild) the concept graph for ``book_id``.

        If the book already has LLM-proposed concepts beyond the gold skeleton
        and ``graph_key`` matches a cached build, the cached report is returned
        (idempotent). Otherwise the full Map–Aggregate runs and the result is
        persisted, replacing any prior *proposal-origin* concepts/edges (the
        gold skeleton is always re-seeded and preserved).
        """
        proj = self.repo.assert_project_owned_by(project_id, learner_id)
        if book_id not in self.repo.allowed_book_ids(project_id):
            raise ScopeError(f"book {book_id} not in project {project_id} scope")

        cache_key = (book_id, graph_key)
        if graph_key and cache_key in self._cache:
            report = self._cache[cache_key]
            report.reused = True
            return report

        # Gather chunks for this book (prefer explicit arg, else the repo).
        book_chunks = chunks if chunks is not None else self.repo.chunks_for_book(book_id)
        sections = self._sections_with_chunks(parsed_document, book_chunks)
        source = self.repo.get_source(book_id)
        has_nested_outline = any(len(section.section_path) >= 2 for section in sections)
        sections = [
            section for section in sections
            if is_learning_section(
                section.title,
                section.section_path,
                book_title=source.title if source else "",
                require_leaf=has_nested_outline,
            )
        ]

        # The hand-authored Java skeleton belongs only to the bundled demo.
        # Real uploads must be mapped from their own text, never contaminated
        # with the demo subject matter.
        is_demo_book = book_id == DEMO_BOOK_ID
        gold_concepts = build_skeleton(book_id) if is_demo_book else []
        gold_relations = self._gold_relations(book_id) if is_demo_book else []
        known_names = [c.name for c in gold_concepts]

        # 1. Per-section proposals.
        section_proposals = []
        fallback_sections = 0
        # A detailed textbook outline already supplies enough stable learning
        # units. Sentence-led fallback extraction across hundreds of sections
        # produced hundreds of OCR-fragment concepts, making the graph and
        # practice queue unusable.
        extract_definitions = len(sections) <= 80
        for sec in sections:
            sec_chunks = [c for c in book_chunks if _chunk_in_section(c, sec)]
            if not sec_chunks:
                continue
            sp = self.mapper.map_section(
                sec,
                sec_chunks,
                known_concept_names=known_names,
                extract_definitions=extract_definitions,
            )
            if sp.fallback:
                fallback_sections += 1
            section_proposals.append(sp)
            # Let later sections reuse names proposed earlier this run too.
            known_names = list({*known_names, *[c.name for c in sp.concepts]})

        # 2. Deterministic merge + validation.
        build = build_book_graph(
            book_id=book_id, gold_concepts=gold_concepts, gold_relations=gold_relations,
            section_proposals=section_proposals,
        )

        # 3. Persist: replace proposal-origin concepts/edges, keep gold.
        self._persist(book_id, build, gold_concepts)

        # 4. Report + diff for undo.
        diff = diff_from_build(build)
        gold_count = sum(1 for c in build.concepts if c.source == ConceptSource.GOLD.value)
        new_count = len(build.concepts) - gold_count
        prereq_edges = [r for r in build.relations if r.relation == RelationType.PREREQUISITE]
        report = MappingReport(
            book_id=book_id, total_concepts=len(build.concepts),
            gold_concepts=gold_count, new_concepts=new_count,
            total_prereq_edges=len(prereq_edges),
            dropped_edges=build.dropped_edges, gold_protected=build.gold_protected,
            fallback_sections=fallback_sections,
            added_concept_ids=diff.added_concept_ids, added_edges=diff.added_edges,
        )
        if graph_key:
            self._cache[cache_key] = report
        return report

    # --- undo -------------------------------------------------------------

    def undo_mapping(
        self,
        *,
        project_id: str,
        learner_id: str,
        book_id: str,
        report: MappingReport,
    ) -> dict:
        """Remove the proposal-origin concepts/edges recorded in ``report``.

        Gold skeleton concepts and edges are untouched. Learner state is
        untouched (we never delete concepts that have state unless they were
        proposal-origin and never used — caller passes the same report).
        Returns a summary of what was removed.
        """
        self.repo.assert_project_owned_by(project_id, learner_id)
        if book_id not in self.repo.allowed_book_ids(project_id):
            raise ScopeError(f"book {book_id} not in project {project_id} scope")

        removed_concepts = 0
        removed_edges = 0
        # Remove proposal-origin concepts by id.
        current = self.repo.concepts_for_book(book_id)
        gold_ids = set(skeleton_concept_ids())
        to_remove = [cid for cid in report.added_concept_ids if cid not in gold_ids]
        keep = []
        for c in current:
            if c.concept_id in to_remove and c.source != ConceptSource.GOLD.value:
                removed_concepts += 1
                continue
            keep.append(c)
        edge_set = set(report.added_edges)
        # Rewrite the book's concept list.
        kept_relations = [
            r for r in self.repo.relations_for_book(book_id)
            if (r.source_concept_id, r.target_concept_id) not in edge_set
        ]
        # Remove proposal-origin prerequisite edges from surviving concepts.
        for c in keep:
            if c.source == ConceptSource.GOLD.value:
                continue
            new_pre = [p for p in c.prerequisites if (c.concept_id, p) not in edge_set]
            if len(new_pre) != len(c.prerequisites):
                removed_edges += len(c.prerequisites) - len(new_pre)
                c.prerequisites = new_pre
        # Drop relations that were proposal-origin and in the diff.
        removed_edges += len(self.repo.relations_for_book(book_id)) - len(kept_relations)
        self.repo.replace_book_graph(book_id, keep, kept_relations)
        return {"removed_concepts": removed_concepts, "removed_edges": removed_edges}

    # --- helpers ----------------------------------------------------------

    def _sections_with_chunks(
        self, parsed_document: ParsedDocument | None, chunks: list[DocumentChunk],
    ) -> list[Section]:
        """Return sections to map. If a ParsedDocument is given, use its
        sections; else synthesise one Section per distinct section_path in the
        chunks (so mapping works from chunks alone, as in the demo corpus)."""
        if parsed_document is not None and parsed_document.sections:
            return list(parsed_document.sections)
        seen: dict[tuple[str, ...], Section] = {}
        for i, c in enumerate(chunks):
            path = tuple(c.section_path)
            if not path:
                path = ("全文",)
            if path not in seen:
                seen[path] = Section(
                    section_id=f"sec-{i}", title=path[-1], section_path=path,
                    physical_page=c.source_ref.physical_page,
                )
        return list(seen.values())

    def _gold_relations(self, book_id: str) -> list[ConceptRelation]:
        rels = []
        for cid, prereqs in PREREQUISITE_EDGES.items():
            for p in prereqs:
                rels.append(ConceptRelation(
                    source_concept_id=cid, target_concept_id=p,
                    relation=RelationType.PREREQUISITE, source=ConceptSource.GOLD.value,
                ))
        return rels

    def _persist(self, book_id: str, build: BookGraphBuild, gold_concepts: list[Concept]) -> None:
        """Replace the book's concepts + relations with the build output.

        Gold concepts are always present (re-seeded from the skeleton). Any
        prior proposal-origin concepts are discarded — the build is the new
        source of truth. Learner state keys by concept_id and is untouched;
        state for removed proposal concepts simply becomes orphaned (harmless:
        the decision loop only iterates over current concepts).
        """
        self.repo.replace_book_graph(book_id, list(build.concepts), list(build.relations))


def _chunk_in_section(c: DocumentChunk, sec: Section) -> bool:
    if sec.section_path == ("全文",) and not c.section_path:
        return True
    if c.section_path and sec.section_path and tuple(c.section_path) == tuple(sec.section_path):
        return True
    # Fallback: chunk's first path element matches the section title.
    return bool(c.section_path) and c.section_path[-1] == sec.title


__all__ = ["BookMappingService", "MappingReport"]
