"""Bug Library — Oppia-style misconception entries.

Each entry is specific enough that, from the description alone, one can
predict a typical wrong answer (OPEN_SOURCE_REFERENCES.md §3 acceptance). The
Diagnostician may rephrase examples but must not change the hypotheses a probe
discriminates between.

The five core Java/OOP bugs cover the PRODUCT_SPEC.md §4.2 "首个高质量场景":
  - reference vs object confusion
  - == vs equals
  - equals without hashCode
  - inheritance/interface/polymorphism misuse
  - collection framework prerequisite confusion
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Remediation:
    explanation_goal: str
    positive_example: str
    counterexample: str


@dataclass(frozen=True)
class BugEntry:
    bug_id: str
    description: str
    related_concepts: list[str]
    prerequisite_concepts: list[str]
    observable_error_pattern: str
    likely_wrong_answers: list[str]
    competing_hypotheses: list[str]
    probe_templates: list[str]
    expected_patterns: dict[str, str]  # hypothesis → expected answer pattern
    remediation: Remediation
    changed_task_templates: list[str]
    rubric: list[str]
    source_notes: str
    version: str = "v1"


# --------------------------------------------------------------------------
# Bug 1: reference vs object confusion
# --------------------------------------------------------------------------
BUG_REF_VS_OBJECT = BugEntry(
    bug_id="bug_ref_vs_object",
    description=(
        "Learner treats a reference variable as the object itself, believing "
        "assignment copies the object's state or that two references to the "
        "same object are independent copies."
    ),
    related_concepts=["c_variable", "c_reference", "c_object", "c_mutable_state"],
    prerequisite_concepts=["c_variable"],
    observable_error_pattern=(
        "After `Box a = new Box(); Box b = a; b.setValue(5);` the learner "
        "predicts `a.getValue()` is still the original value."
    ),
    likely_wrong_answers=[
        "a.getValue() returns the original value because b is a separate copy.",
        "Assigning b = a copies all fields from a into a new Box.",
    ],
    competing_hypotheses=[
        "h_ref_aliasing: learner does not understand reference aliasing (a and b point to the same object).",
        "h_value_semantics: learner assumes Java has value-copy semantics for objects.",
    ],
    probe_templates=[
        "Given `Box a = new Box(1); Box b = a; b.setValue(9);` what is `a.getValue()` and why?",
    ],
    expected_patterns={
        "h_ref_aliasing": "Answers 9 but explains it as 'they share memory' without naming reference/aliasing.",
        "h_value_semantics": "Answers the original value (1), claiming b is a copy.",
    },
    remediation=Remediation(
        explanation_goal=(
            "A reference variable stores the address of an object, not the object's "
            "fields. `b = a` copies the reference, so both names point to the SAME object; "
            "mutating through either is visible through both."
        ),
        positive_example=(
            "Box a = new Box(1); Box b = a; b.setValue(9); // a.getValue() == 9, same object"
        ),
        counterexample=(
            "int x = 1; int y = x; y = 9; // x == 1, primitives ARE copied by value"
        ),
    ),
    changed_task_templates=[
        "Two Student references share one object; update grade via one, read via the other (near transfer).",
        "A List field is mutated through a returned reference; predict whether the caller sees the change (far transfer).",
    ],
    rubric=[
        "Correctly identifies that a and b refer to the same object.",
        "Explains that reference assignment does not copy fields.",
        "Distinguishes reference (object) vs primitive (value) assignment.",
    ],
    source_notes="Oppia-style Specific Error; Java OOP textbook.",
)


# --------------------------------------------------------------------------
# Bug 2: == vs equals
# --------------------------------------------------------------------------
BUG_EQ_VS_EQUALS = BugEntry(
    bug_id="bug_eq_vs_equals",
    description=(
        "Learner uses == to compare string/object content, expecting reference "
        "equality to reflect value equality."
    ),
    related_concepts=["c_reference_equality", "c_value_equality", "c_string"],
    prerequisite_concepts=["c_reference"],
    observable_error_pattern=(
        "For `new String(\"a\") == new String(\"a\")` the learner predicts true."
    ),
    likely_wrong_answers=[
        "== compares the contents of two Strings, so it returns true.",
        "== and equals do the same thing for Strings.",
    ],
    competing_hypotheses=[
        "h_eq_is_content: learner thinks == compares content.",
        "h_eq_is_ref_but_unsure: learner knows == is reference but not when interned strings coincide.",
    ],
    probe_templates=[
        "What does `new String(\"hi\") == new String(\"hi\")` return, and how is that different from `.equals`?",
    ],
    expected_patterns={
        "h_eq_is_content": "Predicts true, explains via content equality.",
        "h_eq_is_ref_but_unsure": "Predicts false but cannot explain internment vs new object.",
    },
    remediation=Remediation(
        explanation_goal=(
            "== compares references (do two variables point to the SAME object); "
            ".equals compares logical content (overridable). For Strings always use .equals."
        ),
        positive_example='new String("a").equals(new String("a")) == true; new String("a") == new String("a") == false',
        counterexample='"a" == "a" may be true due to string pooling, but this is NOT guaranteed for new String(...).',
    ),
    changed_task_templates=[
        "Compare two Integer boxes (== vs .equals) for cached vs new instances (near transfer).",
        "A custom Point class: decide == vs equals after override (far transfer).",
    ],
    rubric=[
        "States == compares references, not content.",
        "States .equals compares content (and is overridable).",
        "Explains why new String(...) == new String(...) is false.",
    ],
    source_notes="Java OOP textbook; classic ==/equals confusion.",
)


# --------------------------------------------------------------------------
# Bug 3: equals without hashCode
# --------------------------------------------------------------------------
BUG_EQUALS_HASHCODE = BugEntry(
    bug_id="bug_equals_no_hashcode",
    description=(
        "Learner overrides equals but not hashCode (or inconsistently), breaking "
        "the contract that equal objects must have equal hash codes."
    ),
    related_concepts=["c_hashcode", "c_equals_contract", "c_hashset_hashmap"],
    prerequisite_concepts=["c_value_equality", "c_hashset_hashmap"],
    observable_error_pattern=(
        "After overriding equals on a Person class, the learner expects a HashSet "
        "to deduplicate two equal Person objects — but it does not."
    ),
    likely_wrong_answers=[
        "HashSet will remove duplicates as long as equals returns true, hashCode doesn't matter.",
        "hashCode is only used for security, not for collections.",
    ],
    competing_hypotheses=[
        "h_hashcode_unused: learner thinks hashCode is irrelevant to HashSet/HashMap.",
        "h_hashcode_default_ok: learner thinks the default hashCode is consistent with their equals.",
    ],
    probe_templates=[
        "You override equals on Person but not hashCode. Two Person objects that are equals() are added to a HashSet. How many elements remain, and why?",
    ],
    expected_patterns={
        "h_hashcode_unused": "Says one element remains because equals handles dedup.",
        "h_hashcode_default_ok": "Says one element but assumes default hashCode already matches equals.",
    },
    remediation=Remediation(
        explanation_goal=(
            "HashSet/HashMap use hashCode to pick a bucket, then equals within the bucket. "
            "If equal objects have different hash codes they land in different buckets and are "
            "never compared. The contract: equal objects MUST have equal hash codes."
        ),
        positive_example="Override both: equals compares id; hashCode returns id.hashCode().",
        counterexample="Override equals only → two equal objects get distinct default hashCodes → HashSet keeps both.",
    ),
    changed_task_templates=[
        "Fix a Person class so a HashSet deduplicates correctly (near transfer).",
        "Diagnose why a HashMap lookup misses a key whose equals matches (far transfer).",
    ],
    rubric=[
        "States hashCode determines the bucket.",
        "States equals is only called within a bucket.",
        "States the contract: equal objects → equal hash codes.",
    ],
    source_notes="Effective Java-style contract.",
)


# --------------------------------------------------------------------------
# Bug 4: polymorphism / dynamic dispatch misuse
# --------------------------------------------------------------------------
BUG_POLYMORPHISM_DISPATCH = BugEntry(
    bug_id="bug_static_dispatch",
    description=(
        "Learner expects static (compile-time) method dispatch instead of dynamic "
        "(runtime) dispatch, or confuses overriding with overloading."
    ),
    related_concepts=["c_inheritance", "c_polymorphism", "c_override_vs_overload", "c_dynamic_dispatch"],
    prerequisite_concepts=["c_inheritance"],
    observable_error_pattern=(
        "Given `Animal a = new Dog(); a.speak();` where Dog overrides speak, the "
        "learner predicts the Animal version runs because 'the type is Animal'."
    ),
    likely_wrong_answers=[
        "Animal.speak() runs because the declared type is Animal.",
        "Overriding is decided at compile time.",
    ],
    competing_hypotheses=[
        "h_static_binding: learner thinks method calls bind to the declared type.",
        "h_overload_override_confuse: learner confuses overloading (compile-time) with overriding (runtime).",
    ],
    probe_templates=[
        "Animal a = new Dog(); a.speak(); — which speak() runs, and is the decision made at compile time or runtime?",
    ],
    expected_patterns={
        "h_static_binding": "Predicts Animal.speak(), cites declared type.",
        "h_overload_override_confuse": "Predicts Dog.speak() but cannot distinguish override vs overload.",
    },
    remediation=Remediation(
        explanation_goal=(
            "For instance methods, Java uses dynamic dispatch: the JVM looks up the "
            "method on the ACTUAL runtime type of the object. The declared type only "
            "decides which methods are visible at compile time."
        ),
        positive_example="Animal a = new Dog(); a.speak(); // Dog.speak() runs (runtime type is Dog)",
        counterexample="Static methods and field access use the declared type (static binding).",
    ),
    changed_task_templates=[
        "Predict output of a Shape array with overridden area() (near transfer).",
        "Distinguish a static-method call vs overridden instance method across a hierarchy (far transfer).",
    ],
    rubric=[
        "Identifies that the runtime type decides which override runs.",
        "Distinguishes overriding (runtime) from overloading (compile-time).",
        "Notes that static methods/fields use static binding.",
    ],
    source_notes="Java OOP textbook, polymorphism chapter.",
)


# --------------------------------------------------------------------------
# Bug 5: collection framework prerequisite confusion
# --------------------------------------------------------------------------
BUG_COLLECTION_PREREQ = BugEntry(
    bug_id="bug_collection_choice",
    description=(
        "Learner picks the wrong collection for a requirement because the "
        "interface/implementation hierarchy and performance contracts are unclear."
    ),
    related_concepts=["c_list_vs_set", "c_map", "c_ordering", "c_duplicates"],
    prerequisite_concepts=["c_hashset_hashmap", "c_hashcode"],
    observable_error_pattern=(
        "Asked to store unique items preserving insertion order, the learner chooses HashSet."
    ),
    likely_wrong_answers=[
        "HashSet preserves insertion order.",
        "Use ArrayList and manually check contains() — O(1) lookup.",
    ],
    competing_hypotheses=[
        "h_set_unordered: learner does not know HashSet is unordered.",
        "h_contains_is_cheap: learner thinks ArrayList.contains is O(1).",
    ],
    probe_templates=[
        "You need a collection of unique elements that keeps the order they were added. Which do you choose and why? What is the cost of membership checks?",
    ],
    expected_patterns={
        "h_set_unordered": "Chooses HashSet, claims it keeps insertion order.",
        "h_contains_is_cheap": "Chooses ArrayList, claims contains() is O(1).",
    },
    remediation=Remediation(
        explanation_goal=(
            "HashSet is unordered (O(1) membership but no insertion order). LinkedHashSet "
            "gives O(1) membership AND insertion order. ArrayList.contains is O(n)."
        ),
        positive_example="LinkedHashSet keeps insertion order with O(1) contains.",
        counterexample="HashSet iterates in hash order, not insertion order.",
    ),
    changed_task_templates=[
        "Pick the right collection for a dedup-with-order task (near transfer).",
        "Given a performance profile, choose between TreeSet/HashSet/LinkedHashSet (far transfer).",
    ],
    rubric=[
        "Identifies HashSet as unordered.",
        "Identifies LinkedHashSet as insertion-ordered with O(1) contains.",
        "States ArrayList.contains is O(n).",
    ],
    source_notes="Java Collections Framework chapter.",
)


# --------------------------------------------------------------------------
# Bug 6: queue FIFO vs stack LIFO confusion
# --------------------------------------------------------------------------
# ``c_queue`` is only a semantic anchor.  On imported textbooks TaskService
# maps this entry to the actual in-scope Queue section, so no book-specific
# concept id is hard-coded into a learner's state.
BUG_QUEUE_FIFO_LIFO = BugEntry(
    bug_id="bug_queue_fifo_lifo",
    description=(
        "Learner confuses a queue's first-in-first-out discipline with a "
        "stack's last-in-first-out discipline, commonly deleting from the "
        "tail after enqueueing at the tail."
    ),
    related_concepts=["c_queue"],
    prerequisite_concepts=[],
    observable_error_pattern=(
        "After A, B, C are enqueued in that order, the learner says dequeue "
        "returns C or removes the rear element."
    ),
    likely_wrong_answers=[
        "出队从队尾删除最后入队的元素。",
        "先入队 A、B、C 后，出队得到 C。",
        "队列和栈一样都是后进先出。",
        "从队尾出队。",
    ],
    competing_hypotheses=[
        "h_lifo_transfer: learner transfers the stack's LIFO rule to a queue.",
        "h_end_role_confusion: learner knows FIFO verbally but swaps front and rear operations.",
    ],
    probe_templates=[
        "空队列依次入队 A、B、C 后，连续两次出队分别得到什么？请标出队首、队尾，并解释每次删除发生在哪一端。",
    ],
    expected_patterns={
        "h_lifo_transfer": "Answers C then B, or explicitly says the newest item leaves first.",
        "h_end_role_confusion": "Answers A then B but says dequeue removes from the rear or labels front/rear backwards.",
    },
    remediation=Remediation(
        explanation_goal=(
            "队列把两端职责分开：enqueue 在队尾加入新元素，dequeue 从队首取出最早到达的元素。"
            "用排队取号的时间顺序核对，而不是把栈顶操作迁移过来。"
        ),
        positive_example=(
            "依次入队 A、B、C：队首为 A、队尾为 C；出队两次依次得到 A、B，剩下 C。"
        ),
        counterexample=(
            "若从队尾取 C，就变成后进先出；这描述的是栈，不是普通队列。"
        ),
    ),
    changed_task_templates=[
        "医院叫号队列当前依次为 101、102、103。新来 104 后叫号两次；写出两次被服务者、剩余队列，并说明新来者插入的位置。",
        "BFS 从顶点 S 开始，按发现顺序将 A、B、C 入队。若每次从队首取出一个顶点，前三次取出的顶点是什么？为什么不能先处理 C？",
    ],
    rubric=[
        "明确队列遵循先进先出（FIFO），而不是栈的后进先出。",
        "正确说明入队发生在队尾、出队发生在队首。",
        "能按操作先后顺序给出正确的出队结果或解释其原因。",
    ],
    source_notes="数据结构教材：队列 ADT 与 FIFO 操作语义。",
)


BUG_LIBRARY: dict[str, BugEntry] = {
    b.bug_id: b for b in [
        BUG_REF_VS_OBJECT,
        BUG_EQ_VS_EQUALS,
        BUG_EQUALS_HASHCODE,
        BUG_POLYMORPHISM_DISPATCH,
        BUG_COLLECTION_PREREQ,
        BUG_QUEUE_FIFO_LIFO,
    ]
}


def get_bug(bug_id: str) -> BugEntry:
    return BUG_LIBRARY[bug_id]
