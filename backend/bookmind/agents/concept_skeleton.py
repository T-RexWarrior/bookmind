"""Java/OOP core concept skeleton — ~30 gold concepts with prerequisites.

ROADMAP Phase 0: "Java 教材 30 个核心 concept 骨架". These are the human-curated
gold edges; the Book Mapper may extend to 50–80 but must NOT overwrite these
(LEARNING_MODEL.md §2 rule 5). Concepts are grouped by chapter, with
prerequisite edges forming a DAG (acyclic — the Engine validates this).
"""

from __future__ import annotations

from ..domain.enums import ConceptSource, Difficulty
from ..domain.models import Concept


# (concept_id, name, chapter, importance, difficulty, prerequisites)
_SKELETON: list[tuple[str, str, str, float, Difficulty, list[str]]] = [
    # --- Fundamentals ---
    ("c_variable", "Variables & primitive types", "Fundamentals", 0.9, Difficulty.EASY, []),
    ("c_reference", "References vs values", "Fundamentals", 0.95, Difficulty.MEDIUM, ["c_variable"]),
    ("c_object", "Objects & instantiation", "Fundamentals", 0.9, Difficulty.MEDIUM, ["c_reference"]),
    ("c_mutable_state", "Mutable object state", "Fundamentals", 0.8, Difficulty.MEDIUM, ["c_object"]),
    ("c_scope_lifetime", "Scope & lifetime", "Fundamentals", 0.7, Difficulty.EASY, ["c_variable"]),

    # --- Methods & control ---
    ("c_method", "Methods & parameters", "Methods", 0.85, Difficulty.EASY, ["c_variable"]),
    ("c_pass_by_value", "Pass-by-value semantics", "Methods", 0.85, Difficulty.MEDIUM, ["c_reference", "c_method"]),
    ("c_return", "Return values", "Methods", 0.6, Difficulty.EASY, ["c_method"]),
    ("c_control_flow", "Control flow basics", "Methods", 0.6, Difficulty.EASY, ["c_variable"]),

    # --- Strings & equality ---
    ("c_string", "String & immutability", "Strings", 0.8, Difficulty.MEDIUM, ["c_reference"]),
    ("c_reference_equality", "== operator (reference)", "Strings", 0.85, Difficulty.MEDIUM, ["c_reference", "c_string"]),
    ("c_value_equality", "equals() for content", "Strings", 0.9, Difficulty.MEDIUM, ["c_reference_equality"]),
    ("c_equals_contract", "equals contract", "Strings", 0.8, Difficulty.HARD, ["c_value_equality"]),
    ("c_hashcode", "hashCode()", "Strings", 0.85, Difficulty.HARD, ["c_equals_contract"]),

    # --- Inheritance & polymorphism ---
    ("c_class", "Classes & encapsulation", "Inheritance", 0.9, Difficulty.MEDIUM, ["c_object"]),
    ("c_inheritance", "Inheritance", "Inheritance", 0.9, Difficulty.MEDIUM, ["c_class"]),
    ("c_override_vs_overload", "Override vs overload", "Inheritance", 0.8, Difficulty.HARD, ["c_inheritance", "c_method"]),
    ("c_polymorphism", "Polymorphism", "Inheritance", 0.95, Difficulty.HARD, ["c_inheritance"]),
    ("c_dynamic_dispatch", "Dynamic dispatch", "Inheritance", 0.85, Difficulty.HARD, ["c_polymorphism"]),
    ("c_abstract_class", "Abstract classes", "Inheritance", 0.75, Difficulty.MEDIUM, ["c_inheritance"]),

    # --- Interfaces ---
    ("c_interface", "Interfaces", "Interfaces", 0.9, Difficulty.MEDIUM, ["c_abstract_class"]),
    ("c_default_methods", "Default methods", "Interfaces", 0.55, Difficulty.MEDIUM, ["c_interface"]),
    ("c_implements_multiple", "Multiple interface implementation", "Interfaces", 0.7, Difficulty.MEDIUM, ["c_interface"]),

    # --- Collections ---
    ("c_collection_hierarchy", "Collection hierarchy", "Collections", 0.85, Difficulty.MEDIUM, ["c_interface"]),
    ("c_list_vs_set", "List vs Set semantics", "Collections", 0.85, Difficulty.MEDIUM, ["c_collection_hierarchy"]),
    ("c_map", "Map & key-value", "Collections", 0.85, Difficulty.MEDIUM, ["c_collection_hierarchy"]),
    ("c_hashset_hashmap", "HashSet/HashMap & hashing", "Collections", 0.9, Difficulty.HARD, ["c_map", "c_hashcode"]),
    ("c_ordering", "Ordering guarantees", "Collections", 0.7, Difficulty.MEDIUM, ["c_list_vs_set"]),
    ("c_duplicates", "Duplicate handling", "Collections", 0.65, Difficulty.EASY, ["c_list_vs_set"]),
    ("c_iteration", "Iteration & for-each", "Collections", 0.6, Difficulty.EASY, ["c_collection_hierarchy"]),
]

# Map concept_id → its prerequisite concept_ids (the gold edges).
PREREQUISITE_EDGES: dict[str, list[str]] = {cid: prereqs for cid, _, _, _, _, prereqs in _SKELETON}


def build_skeleton(book_id: str) -> list[Concept]:
    """Instantiate the gold skeleton as Concept models for a given book."""
    concepts: list[Concept] = []
    for cid, name, chapter, importance, difficulty, prereqs in _SKELETON:
        concepts.append(
            Concept(
                concept_id=cid,
                book_id=book_id,
                name=name,
                chapter=chapter,
                section="",
                importance=importance,
                difficulty=difficulty,
                source=ConceptSource.GOLD.value,
                prerequisites=list(prereqs),
                goal_relevance=importance,  # skeleton goals mirror importance by default
            )
        )
    return concepts


def skeleton_concept_ids() -> list[str]:
    return [cid for cid, *_ in _SKELETON]
