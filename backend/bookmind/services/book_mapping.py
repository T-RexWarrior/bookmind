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
from ..domain.proposals import ConceptProposal, SectionProposal
from ..domain.source_ref import SourceRef
from ..engine.book_graph.mapper import BookGraphBuild, build_book_graph, diff_from_build
from ..llm.router import ModelRouter
from ..retrieval.chunk import DocumentChunk
from ..retrieval.parsed_document import ParsedDocument, Section
from ..storage.protocols import Repository, ScopeError
from .concept_scope import is_learning_section, normalise_section_name


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
    migrated_states: int = 0
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
        #
        # A persisted graph node is a learning unit, not every noun that
        # occurs in the prose.  For an uploaded book, the smallest reliable
        # heading is the only writable unit.  Terms inside it remain query
        # aliases/anchors; automatically promoting them to mastery nodes is
        # non-deterministic and makes a single answer look more precise than
        # the evidence actually is.  This also makes real-book mapping cost
        # zero LLM calls.
        #
        # The bundled demo keeps its hand-authored skeleton and mapper for its
        # established pedagogical contract.  It is never used for an uploaded
        # textbook.
        section_proposals = []
        fallback_sections = 0
        for sec in sections:
            sec_chunks = [c for c in book_chunks if _chunk_in_section(c, sec)]
            if not sec_chunks:
                continue
            if is_demo_book:
                sp = self.mapper.map_section(
                    sec,
                    sec_chunks,
                    known_concept_names=known_names,
                    extract_definitions=len(sections) <= 80,
                )
                if sp.fallback:
                    fallback_sections += 1
            else:
                sp = _learning_unit_from_section(sec, sec_chunks)
            section_proposals.append(sp)
            if is_demo_book:
                # Later demo sections may reuse earlier proposed names. Real
                # heading units must remain independent and deterministic.
                known_names = list({*known_names, *[c.name for c in sp.concepts]})

        # 2. Deterministic merge + validation.
        build = build_book_graph(
            book_id=book_id, gold_concepts=gold_concepts, gold_relations=gold_relations,
            section_proposals=section_proposals,
        )

        # 3. Persist: replace proposal-origin concepts/edges, keep gold.
        migrated_states = self._persist(book_id, build, gold_concepts)

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
            migrated_states=migrated_states,
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

    def _persist(self, book_id: str, build: BookGraphBuild, gold_concepts: list[Concept]) -> int:
        """Replace the book's concepts + relations with the build output.

        Gold concepts are always present (re-seeded from the skeleton). Any
        prior proposal-origin concepts are discarded — the build is the new
        source of truth. A meaningful learner state transfers only when its
        old and new units have the exact same smallest-section identity;
        broad/noun-only legacy nodes never receive a guessed migration.
        """
        previous = self.repo.concepts_for_book(book_id)
        self.repo.replace_book_graph(book_id, list(build.concepts), list(build.relations))
        return self._migrate_states_by_section(book_id, previous, list(build.concepts))

    def _migrate_states_by_section(
        self, book_id: str, previous: list[Concept], replacement: list[Concept],
    ) -> int:
        """Copy state only across a one-to-one, exact section match.

        Evidence stays append-only under its original concept id. This is
        intentionally a state-view migration, not a rewrite of historical
        evidence; ambiguous old aggregate nodes are left as history rather
        than being falsely credited to a new section.
        """
        old_by_key = _unique_concepts_by_section(previous)
        new_by_key = _unique_concepts_by_section(replacement)
        moves = {
            old_by_key[key].concept_id: new_by_key[key].concept_id
            for key in old_by_key.keys() & new_by_key.keys()
            if old_by_key[key].concept_id != new_by_key[key].concept_id
        }
        if not moves:
            return 0
        migrated = 0
        for project_id in self.repo.project_ids_for_book(book_id):
            states = {state.concept_id: state for state in self.repo.states_for_project(project_id)}
            for old_id, new_id in moves.items():
                old_state = states.get(old_id)
                if old_state is None or not _state_has_learning_signal(old_state):
                    continue
                target_state = states.get(new_id)
                if target_state is not None and _state_has_learning_signal(target_state):
                    continue
                migrated_state = old_state.model_copy(update={"concept_id": new_id}, deep=True)
                self.repo.save_state(migrated_state)
                states[new_id] = migrated_state
                migrated += 1
        return migrated


def _chunk_in_section(c: DocumentChunk, sec: Section) -> bool:
    if sec.section_path == ("全文",) and not c.section_path:
        return True
    if c.section_path and sec.section_path and tuple(c.section_path) == tuple(sec.section_path):
        return True
    # Fallback: chunk's first path element matches the section title.
    return bool(c.section_path) and c.section_path[-1] == sec.title


def _learning_unit_from_section(sec: Section, chunks: list[DocumentChunk]) -> SectionProposal:
    """Create one deterministic, source-backed learning unit for a heading."""
    refs: list[SourceRef] = []
    seen: set[str] = set()
    for chunk in chunks:
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        refs.append(SourceRef(
            document_id=chunk.source_ref.document_id,
            chunk_id=chunk.chunk_id,
            block_id=chunk.source_ref.block_id,
            physical_page=chunk.source_ref.physical_page,
            section_path=chunk.section_path or sec.section_path,
        ))
    # Retaining § numbers prevents repeated labels such as “小结” or “实验”
    # from collapsing into one book-wide concept identity.
    name = (sec.section_path[-1] if sec.section_path else sec.title).strip()
    return SectionProposal(
        section_id=sec.section_id,
        section_path=sec.section_path,
        concepts=[ConceptProposal(
            name=name,
            description=f"教材学习单元：{' · '.join(sec.section_path)}",
            chapter=sec.section_path[0] if sec.section_path else "",
            section=" · ".join(sec.section_path),
            importance=0.6,
            source_refs=refs,
            rationale="deterministic: smallest reliable textbook heading",
        )],
        relations=[],
        fallback=False,
    )


def _unique_concepts_by_section(concepts: list[Concept]) -> dict[str, Concept]:
    """Keep only unambiguous smallest-section identities for migration."""
    grouped: dict[str, list[Concept]] = {}
    for concept in concepts:
        key = _smallest_section_key(concept)
        if key:
            grouped.setdefault(key, []).append(concept)
    return {key: items[0] for key, items in grouped.items() if len(items) == 1}


def _smallest_section_key(concept: Concept) -> str:
    """Stable path identity; rejects chapter-wide and cross-section nodes."""
    paths = {
        tuple(ref.section_path)
        for ref in concept.source_refs
        if ref.section_path
    }
    if len(paths) != 1:
        return ""
    path = next(iter(paths))
    if not is_learning_section(path[-1], path, require_leaf=True):
        return ""
    return " · ".join(normalise_section_name(item) for item in path)


def _state_has_learning_signal(state) -> bool:
    if state.read_progress > 0 or state.goal_relevance > 0:
        return True
    if state.exposure_state.value != "NONE":
        return True
    if state.highest_ever_level.value != "L0" or state.current_verified_level.value != "L0":
        return True
    return any(record.status.value != "UNVERIFIED" for record in state.levels.values())


__all__ = ["BookMappingService", "MappingReport"]
