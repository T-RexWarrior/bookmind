"""Reset local usage data and install a presentation-ready learning space.

This script is intentionally conservative:

* it creates an online SQLite backup before changing anything;
* it keeps users, source metadata, uploaded PDFs, parsed documents and indexes;
* it removes only learning-space usage records;
* it gives every existing anonymous browser identity exactly one equivalent
  demo space, so the demo still appears if the browser has an older cookie.

Run from the BookMind root::

    .venv/Scripts/python.exe scripts/reset_demo_workspace.py --yes
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from bookmind.domain.enums import (  # noqa: E402
    BookRole,
    Difficulty,
    EvidenceResult,
    EvidenceType,
    ExposureState,
    HintLevel,
    Level,
    LevelStatus,
    RelationType,
    UIPreset,
)
from bookmind.domain.models import (  # noqa: E402
    Concept,
    ConceptRelation,
    ContentBlock,
    Conversation,
    Evidence,
    LearnerConceptState,
    LearningProject,
    LevelRecord,
    Message,
    ProjectBook,
)
from bookmind.domain.source_ref import SourceRef  # noqa: E402
from bookmind.jobs.job_store import IngestionJob, JobStage, JobState  # noqa: E402
from bookmind.storage.sql.repository import SqlRepository  # noqa: E402


DB_PATH = (PROJECT_ROOT / "data" / "bookmind.db").resolve()
BACKUP_DIR = (PROJECT_ROOT / "data" / "backups").resolve()


@dataclass(frozen=True)
class DemoConcept:
    concept_id: str
    name: str
    description: str
    chapter: str
    section: str
    page: int
    importance: float
    difficulty: Difficulty = Difficulty.MEDIUM
    prerequisites: tuple[str, ...] = ()
    related: tuple[str, ...] = ()


DEMO_CONCEPTS = (
    DemoConcept(
        "demo_complexity", "算法复杂度与大 O 记号",
        "用渐进记号描述算法规模增长时的时间与空间开销。",
        "第1章 绪论", "§1.2 复杂度度量", 31, 1.0,
        related=("demo_recursion", "demo_binary_search", "demo_quicksort"),
    ),
    DemoConcept(
        "demo_recursion", "递归与递归复杂度",
        "理解递归基、递归调用链及递归算法的复杂度分析。",
        "第1章 绪论", "§1.4 递归", 43, 0.88,
        prerequisites=("demo_complexity",), related=("demo_stack",),
    ),
    DemoConcept(
        "demo_vector_expand", "向量的动态扩容",
        "理解容量不足时的扩容策略与分摊复杂度。",
        "第2章 向量", "§2.4 动态空间管理", 56, 0.92,
        prerequisites=("demo_complexity",), related=("demo_binary_search",),
    ),
    DemoConcept(
        "demo_binary_search", "有序向量与二分查找",
        "利用有序性不断缩小搜索区间，并分析其对数复杂度。",
        "第2章 向量", "§2.6 有序向量", 69, 0.95,
        prerequisites=("demo_complexity",), related=("demo_vector_expand",),
    ),
    DemoConcept(
        "demo_list", "列表与向量的差异",
        "比较顺序存储与链式存储在访问、插入和删除上的权衡。",
        "第3章 列表", "§3.1 从向量到列表", 88, 0.82,
        prerequisites=("demo_vector_expand",), related=("demo_stack", "demo_queue"),
    ),
    DemoConcept(
        "demo_stack", "栈与递归",
        "理解后进先出结构，以及运行栈如何支持递归调用。",
        "第4章 栈与队列", "§4.2 栈与递归", 110, 0.96,
        prerequisites=("demo_recursion",), related=("demo_queue",),
    ),
    DemoConcept(
        "demo_queue", "队列及其应用",
        "理解先进先出语义及队列在层次遍历和图搜索中的作用。",
        "第4章 栈与队列", "§4.6 队列", 127, 0.9,
        related=("demo_tree_traversal", "demo_bfs"),
    ),
    DemoConcept(
        "demo_tree_traversal", "二叉树的层次与遍历",
        "掌握先序、中序、后序和层次遍历的访问顺序与实现思路。",
        "第5章 二叉树", "§5.4 遍历", 155, 1.0, Difficulty.HARD,
        prerequisites=("demo_stack", "demo_queue"), related=("demo_huffman", "demo_avl"),
    ),
    DemoConcept(
        "demo_huffman", "Huffman 编码",
        "通过最优前缀编码理解带权路径长度与贪心构造。",
        "第5章 二叉树", "§5.5 Huffman编码", 158, 0.78, Difficulty.HARD,
        prerequisites=("demo_tree_traversal",),
    ),
    DemoConcept(
        "demo_bfs", "图的广度优先搜索",
        "使用队列按层扩展顶点，理解 BFS 树与最短步数性质。",
        "第6章 图", "§6.4 广度优先搜索", 181, 0.94,
        prerequisites=("demo_queue",), related=("demo_dfs",),
    ),
    DemoConcept(
        "demo_dfs", "图的深度优先搜索",
        "使用递归或栈深入未访问分支，并理解发现与完成时刻。",
        "第6章 图", "§6.5 深度优先搜索", 184, 0.92, Difficulty.HARD,
        prerequisites=("demo_stack",), related=("demo_bfs",),
    ),
    DemoConcept(
        "demo_avl", "AVL 树的平衡",
        "理解平衡因子、失衡类型以及旋转如何恢复树高平衡。",
        "第7章 搜索树", "§7.4 AVL树", 216, 0.9, Difficulty.HARD,
        prerequisites=("demo_tree_traversal",),
    ),
    DemoConcept(
        "demo_hash", "散列表与冲突处理",
        "理解散列函数、装填因子及开放定址或链地址冲突处理。",
        "第9章 词典", "§9.3 散列表", 281, 0.86, Difficulty.HARD,
        prerequisites=("demo_complexity",),
    ),
    DemoConcept(
        "demo_kmp", "KMP 字符串匹配",
        "利用模式串自身信息避免主串指针回退。",
        "第11章 串", "§11.3 KMP算法", 333, 0.8, Difficulty.HARD,
        prerequisites=("demo_complexity",),
    ),
    DemoConcept(
        "demo_quicksort", "快速排序与轴点划分",
        "理解轴点划分、递归子问题与平均及最坏复杂度。",
        "第12章 排序", "§12.1 快速排序", 356, 0.93, Difficulty.HARD,
        prerequisites=("demo_complexity", "demo_recursion"),
    ),
)


def _assert_safe_paths() -> None:
    data_dir = (PROJECT_ROOT / "data").resolve()
    if DB_PATH.parent != data_dir or BACKUP_DIR.parent != data_dir:
        raise RuntimeError("Refusing to operate outside BookMind/data")
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")


def _find_demo_source(connection: sqlite3.Connection) -> sqlite3.Row:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT b.*, p.last_activity_at
        FROM books b
        LEFT JOIN project_books pb ON pb.book_id = b.book_id
        LEFT JOIN learning_projects p ON p.project_id = pb.project_id
        WHERE b.page_count > 0
        ORDER BY
          CASE WHEN b.original_filename <> '' THEN 0 ELSE 1 END,
          COALESCE(p.last_activity_at, p.updated_at, p.created_at) DESC,
          b.created_at DESC
        """
    ).fetchall()
    for row in rows:
        upload = PROJECT_ROOT / "data" / "uploads" / row["owner_user_id"] / row["book_id"] / "source.pdf"
        index = PROJECT_ROOT / "data" / "indexes" / row["book_id"] / "chunks.json"
        if upload.is_file() and index.is_file():
            return row
    raise RuntimeError("No indexed PDF source is available for the demo")


def _backup_database(connection: sqlite3.Connection) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"bookmind-before-demo-{timestamp}.db"
    with sqlite3.connect(backup_path) as destination:
        connection.backup(destination)
    return backup_path


def _clear_usage_records(connection: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "run_events", "runs", "messages", "submissions", "trusted_tasks",
        "conversations", "state_transitions", "misconception_hypotheses",
        "evidence", "learner_concept_states", "expiry_keys", "ingestion_jobs",
        "project_books", "learning_projects",
    )
    before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        with connection:
            for table in tables:
                connection.execute(f"DELETE FROM {table}")
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
    return before


def _chunk_lookup(source_id: str) -> dict[int, dict]:
    path = PROJECT_ROOT / "data" / "indexes" / source_id / "chunks.json"
    chunks = json.loads(path.read_text(encoding="utf-8"))
    by_page: dict[int, dict] = {}
    for chunk in chunks:
        page = int(chunk.get("source_ref", {}).get("physical_page") or 0)
        if page and page not in by_page:
            by_page[page] = chunk
    return by_page


def _source_ref(spec: DemoConcept, by_page: dict[int, dict]) -> SourceRef:
    chunk = by_page.get(spec.page)
    if chunk:
        raw = dict(chunk["source_ref"])
        raw["section_path"] = (spec.chapter, spec.section)
        return SourceRef(**raw)
    return SourceRef(
        document_id="demo-data-structures", physical_page=spec.page,
        section_path=(spec.chapter, spec.section),
    )


def _build_graph(source_id: str, by_page: dict[int, dict]) -> tuple[list[Concept], list[ConceptRelation]]:
    concepts = [
        Concept(
            concept_id=spec.concept_id,
            book_id=source_id,
            name=spec.name,
            description=spec.description,
            chapter=spec.chapter,
            section=spec.section,
            source_refs=[_source_ref(spec, by_page)],
            importance=spec.importance,
            difficulty=spec.difficulty,
            source="MANUAL_CONFIRMED",
            prerequisites=list(spec.prerequisites),
            related_concepts=list(spec.related),
            goal_relevance=spec.importance,
        )
        for spec in DEMO_CONCEPTS
    ]
    relations: list[ConceptRelation] = []
    seen: set[tuple[str, str, str]] = set()
    for spec in DEMO_CONCEPTS:
        for prerequisite in spec.prerequisites:
            key = (prerequisite, spec.concept_id, RelationType.PREREQUISITE.value)
            if key not in seen:
                seen.add(key)
                relations.append(ConceptRelation(
                    source_concept_id=prerequisite,
                    target_concept_id=spec.concept_id,
                    relation=RelationType.PREREQUISITE,
                    source="MANUAL_CONFIRMED",
                    rationale=f"{spec.name}需要先理解前置知识。",
                ))
        for related in spec.related:
            key = (spec.concept_id, related, RelationType.RELATED.value)
            if key not in seen:
                seen.add(key)
                relations.append(ConceptRelation(
                    source_concept_id=spec.concept_id,
                    target_concept_id=related,
                    relation=RelationType.RELATED,
                    source="MANUAL_CONFIRMED",
                    rationale="两个知识点在学习与应用中紧密关联。",
                ))
    return concepts, relations


def _state(
    project_id: str,
    concept_id: str,
    *,
    exposure: ExposureState = ExposureState.SEEN,
    read_progress: float = 0.35,
    status: LevelStatus = LevelStatus.UNVERIFIED,
    verified_at: datetime | None = None,
    review_due_at: datetime | None = None,
    current: Level = Level.L0,
    highest: Level = Level.L0,
    stability: float = 0.0,
    retrievability: float = 0.0,
) -> LearnerConceptState:
    item = LearnerConceptState(
        project_id=project_id,
        concept_id=concept_id,
        exposure_state=exposure,
        read_progress=read_progress,
        highest_ever_level=highest,
        current_verified_level=current,
        goal_relevance=next(c.importance for c in DEMO_CONCEPTS if c.concept_id == concept_id),
    )
    item.set_level_record(Level.L1, LevelRecord(
        status=status,
        verified_at=verified_at,
        stability_days=stability,
        retrievability=retrievability,
        review_due_at=review_due_at,
    ))
    return item


def _evidence(
    project_id: str,
    source_id: str,
    concept_id: str,
    ref: SourceRef,
    ordinal: str,
    occurred_at: datetime,
    *,
    evidence_type: EvidenceType,
    result: EvidenceResult | None = None,
    task_id: str = "",
    summary: str = "",
    independent: bool = False,
) -> Evidence:
    return Evidence(
        evidence_id=f"ev_{project_id[-12:]}_{ordinal}",
        event_key=f"demo:{project_id}:{ordinal}",
        project_id=project_id,
        concept_id=concept_id,
        source_book_id=source_id,
        source_refs=[ref],
        source_chunk_ids=[ref.chunk_id] if ref.chunk_id else [],
        evidence_type=evidence_type,
        required_level=Level.L1 if evidence_type == EvidenceType.VERIFY else Level.L0,
        result=result,
        independent=independent,
        hint_level=HintLevel.NONE,
        task_id=task_id,
        task_version=1 if task_id else 0,
        occurred_at=occurred_at,
        source_session="demo_seed",
        content_summary=summary,
    )


def _blocks_for_answer(source_id: str, source_title: str, ref: SourceRef) -> list[ContentBlock]:
    return [
        ContentBlock(type="context", data={
            "kind": "answer_context",
            "scope": "当前资料",
            "reason": "已优先检索你正在阅读的教材页，并结合相关段落组织回答。",
            "items": [{"source_id": source_id, "title": source_title, "locator": ref.short_label()}],
        }),
        ContentBlock(
            type="text",
            text=(
                "大 O 记号描述的是输入规模增大时，运行时间增长的上界趋势。"
                "它忽略常数和低阶项，是为了比较算法在大规模输入下的可扩展性；"
                "因此 3n²+5n+2 记为 O(n²)，但这不表示实际运行时间完全相同。"
            ),
        ),
        ContentBlock(
            type="question_signal",
            data={
                "signal_id": "demo-question-complexity",
                "message": "已记录：你对“大 O 记号”有过疑问；这表示待验证，不等于不会。",
                "concepts": [{
                    "concept_id": "demo_complexity",
                    "name": "算法复杂度与大 O 记号",
                    "question_count": 2,
                }],
            },
        ),
        ContentBlock(
            type="citation",
            chunk_id=ref.chunk_id or "",
            quote="复杂度刻画算法执行时间随问题规模增长的渐进趋势。",
            page=str(ref.physical_page),
            book_id=source_id,
            label=f"[1] {source_title} · {ref.short_label()}",
        ),
    ]


def _save_message(
    repo: SqlRepository,
    conversation_id: str,
    message_id: str,
    role: str,
    blocks: list[ContentBlock],
    created_at: datetime,
) -> None:
    repo.save_message(Message(
        message_id=message_id,
        conversation_id=conversation_id,
        role=role,
        content_blocks=blocks,
        created_at=created_at,
    ))


def _task_data(
    *,
    task_id: str,
    project_id: str,
    conversation_id: str,
    learner_id: str,
    concept_id: str,
    ref: SourceRef,
    prompt: str,
    expected: str,
    rubric: list[str],
    created_at: datetime,
) -> dict:
    return {
        "task_id": task_id,
        "project_id": project_id,
        "conversation_id": conversation_id,
        "run_id": "",
        "learner_id": learner_id,
        "task_version": 1,
        "target_concept_ids": [concept_id],
        "evidence_for_levels": [Level.L1.value],
        "rubric": rubric,
        "allowed_resources": [],
        "source_refs": [ref.model_dump(mode="json")],
        "scenario_fingerprint": f"demo-{concept_id}",
        "is_probe": False,
        "discriminated_bug_ids": [],
        "is_changed_task": False,
        "remediation_stage": 0,
        "prompt_text": prompt,
        "expected_answer": expected,
        "distractors": [],
        "status": "ANSWERED",
        "hints_issued": 0,
        "last_submission_id": None,
        "created_at": created_at,
        "expires_at": None,
    }


def _task_card(task_id: str, prompt: str, focus: str, source_id: str, source_title: str, ref: SourceRef, reason: str) -> ContentBlock:
    return ContentBlock(type="task", data={
        "kind": "quiz",
        "task_id": task_id,
        "prompt_text": prompt,
        "focus": focus,
        "source_scope": [{"source_id": source_id, "title": source_title, "locator": ref.short_label()}],
        "generation_reason": reason,
        "status": "ANSWERED",
    })


def _judgment_card(task_id: str, result: str, reason: str, source_id: str, source_title: str, ref: SourceRef) -> ContentBlock:
    return ContentBlock(type="task", data={
        "kind": "judgment",
        "task_id": task_id,
        "judgment": {
            "judgment_status": "DECIDED",
            "result": result,
            "reason": reason,
            "criterion_results": [
                {"criterion_id": "core", "satisfied": result != "FAIL", "note": "核心概念"},
                {"criterion_id": "reason", "satisfied": result == "PASS", "note": "理由完整"},
            ],
        },
        "written": True,
        "next_action_code": "CONTINUE" if result == "PASS" else "TARGETED_PRACTICE",
        "source_scope": [{
            "source_id": source_id,
            "title": source_title,
            "page": ref.physical_page,
            "locator": ref.short_label(),
        }],
    })


def _seed_project(
    repo: SqlRepository,
    learner_id: str,
    source_id: str,
    source_title: str,
    source_hash: str,
    source_filename: str,
    parser_version: str,
    refs: dict[str, SourceRef],
    ordinal: int,
) -> str:
    now = datetime.now(timezone.utc)
    suffix = learner_id.removeprefix("user_")[:12]
    project_id = f"demo_ds_{suffix}"
    repo.create_project(LearningProject(
        project_id=project_id,
        learner_id=learner_id,
        name="数据结构 · 学习演示空间",
        goal="系统掌握核心数据结构，并通过独立作答确认真正理解",
        learning_scope="第1章复杂度、第2章向量、第4章栈与队列、第5章树、第6章图",
        deadline="2026-09-30",
        current_plan="先处理 2 个有疑问知识点，再复习 1 个薄弱知识点和 1 个到期知识点",
        last_source_id=source_id,
        last_source_page=155,
        default_mode=UIPreset.DEEP_LEARNING,
        created_at=now - timedelta(days=12),
        updated_at=now,
        last_activity_at=now,
    ))
    repo.link_book(ProjectBook(project_id=project_id, book_id=source_id, role=BookRole.PRIMARY))
    # The parsed document and index are deliberately retained during reset.
    # Mirror that fact in the project-scoped job view so the source rail shows
    # "ready" instead of the misleading UNKNOWN state.
    repo.save_ingestion_job(IngestionJob(
        job_id=f"job_{suffix}_ready",
        project_id=project_id,
        book_id=source_id,
        source_hash=source_hash,
        filename=source_filename or "source.pdf",
        state=JobState.SUCCEEDED,
        stage=JobStage.DONE,
        progress=1.0,
        attempt=1,
        parser=parser_version or "pypdf",
        parser_version=parser_version or "pypdf_v4",
        chunker_version="chunker_v2",
        embedding_model="persisted-index",
        embedding_dim=0,
        created_at=now - timedelta(days=12),
        updated_at=now - timedelta(days=12),
        parse_key=f"retained:{source_hash[:16]}",
        chunk_key=f"retained:{source_hash[:16]}:chunks",
        index_key=f"retained:{source_hash[:16]}:index",
    ))

    states = [
        _state(project_id, spec.concept_id, read_progress=0.15 + (index % 5) * 0.11)
        for index, spec in enumerate(DEMO_CONCEPTS)
    ]
    state_by_id = {item.concept_id: item for item in states}
    # Three stable concepts.
    for concept_id, days_ago in (("demo_vector_expand", 3), ("demo_binary_search", 2), ("demo_stack", 1)):
        state_by_id[concept_id] = _state(
            project_id, concept_id,
            exposure=ExposureState.COMPLETED, read_progress=1.0,
            status=LevelStatus.VERIFIED,
            verified_at=now - timedelta(days=days_ago),
            review_due_at=now + timedelta(days=4),
            current=Level.L1, highest=Level.L1,
            stability=7.0, retrievability=0.96,
        )
    # One weak concept and one review-due concept.
    state_by_id["demo_tree_traversal"] = _state(
        project_id, "demo_tree_traversal",
        exposure=ExposureState.COMPLETED, read_progress=0.85,
        status=LevelStatus.UNSTABLE,
        verified_at=now - timedelta(days=3), review_due_at=now + timedelta(hours=8),
        current=Level.L0, highest=Level.L1, stability=1.0, retrievability=0.62,
    )
    state_by_id["demo_bfs"] = _state(
        project_id, "demo_bfs",
        exposure=ExposureState.COMPLETED, read_progress=1.0,
        status=LevelStatus.VERIFIED,
        verified_at=now - timedelta(days=15), review_due_at=now - timedelta(days=2),
        current=Level.L1, highest=Level.L1, stability=3.0, retrievability=0.76,
    )
    for state in state_by_id.values():
        repo.save_state(state)

    task_stack = f"task_{suffix}_stack"
    task_tree = f"task_{suffix}_tree"
    task_bfs = f"task_{suffix}_bfs"
    evidence = [
        _evidence(project_id, source_id, "demo_complexity", refs["demo_complexity"], "q1", now - timedelta(days=8), evidence_type=EvidenceType.QUESTION, summary="question:为什么大 O 要忽略常数项？"),
        _evidence(project_id, source_id, "demo_complexity", refs["demo_complexity"], "q2", now - timedelta(days=5), evidence_type=EvidenceType.QUESTION, summary="question:O(n²) 是否等于实际运行 n² 秒？"),
        _evidence(project_id, source_id, "demo_avl", refs["demo_avl"], "q3", now - timedelta(days=1), evidence_type=EvidenceType.QUESTION, summary="question:AVL 旋转后为什么仍然保持搜索树顺序？"),
        _evidence(project_id, source_id, "demo_vector_expand", refs["demo_vector_expand"], "v1", now - timedelta(days=3), evidence_type=EvidenceType.VERIFY, result=EvidenceResult.PASS, task_id=f"task_{suffix}_vector", summary="独立说明了倍增扩容的分摊复杂度。", independent=True),
        _evidence(project_id, source_id, "demo_binary_search", refs["demo_binary_search"], "v2", now - timedelta(days=2), evidence_type=EvidenceType.VERIFY, result=EvidenceResult.PASS, task_id=f"task_{suffix}_binary", summary="正确解释了二分查找区间不变量。", independent=True),
        _evidence(project_id, source_id, "demo_stack", refs["demo_stack"], "v3", now - timedelta(days=1), evidence_type=EvidenceType.VERIFY, result=EvidenceResult.PASS, task_id=task_stack, summary="正确说明运行栈保存返回地址和局部状态。", independent=True),
        _evidence(project_id, source_id, "demo_tree_traversal", refs["demo_tree_traversal"], "v4", now - timedelta(hours=18), evidence_type=EvidenceType.VERIFY, result=EvidenceResult.PARTIAL, task_id=task_tree, summary="能给出遍历结果，但混淆了层次遍历所用的数据结构。", independent=True),
        _evidence(project_id, source_id, "demo_bfs", refs["demo_bfs"], "v5", now - timedelta(days=15), evidence_type=EvidenceType.VERIFY, result=EvidenceResult.PASS, task_id=task_bfs, summary="曾正确说明 BFS 使用队列，现在已到复习时间。", independent=True),
    ]
    for item in evidence:
        repo.append_evidence(item)

    learn_id = f"conv_{suffix}_learn"
    review_id = f"conv_{suffix}_review"
    assess_id = f"conv_{suffix}_assess"
    conversations = [
        Conversation(conversation_id=learn_id, project_id=project_id, activity_type="LEARN", title="复杂度：边读边问", created_at=now - timedelta(days=8), updated_at=now - timedelta(days=5)),
        Conversation(conversation_id=review_id, project_id=project_id, activity_type="REVIEW", title="栈与树：练习记录", created_at=now - timedelta(days=2), updated_at=now - timedelta(hours=18)),
        Conversation(conversation_id=assess_id, project_id=project_id, activity_type="REVIEW", title="图搜索：到期复验记录", created_at=now - timedelta(days=15), updated_at=now - timedelta(days=15)),
    ]
    for conversation in conversations:
        repo.save_conversation(conversation)

    _save_message(repo, learn_id, f"msg_{suffix}_l1", "user", [ContentBlock(type="text", text="为什么大 O 记号可以忽略常数项？")], now - timedelta(days=8))
    _save_message(repo, learn_id, f"msg_{suffix}_l2", "assistant", _blocks_for_answer(source_id, source_title, refs["demo_complexity"]), now - timedelta(days=8, seconds=-20))
    _save_message(repo, learn_id, f"msg_{suffix}_l3", "user", [ContentBlock(type="context", data={"kind": "selection_context", "page": 31, "quote": "随着输入规模增大，低阶项和常数因子的影响逐渐减弱。"}), ContentBlock(type="text", text="那 O(n²) 是不是代表一定运行 n² 秒？")], now - timedelta(days=5))
    _save_message(repo, learn_id, f"msg_{suffix}_l4", "assistant", [ContentBlock(type="text", text="不是。O(n²) 描述增长量级，不是秒数；硬件、实现和输入分布仍会影响实际时间。这个追问也已记入同一知识点的疑问记录。"), ContentBlock(type="citation", chunk_id=refs["demo_complexity"].chunk_id or "", page="31", book_id=source_id, label=f"[1] {source_title} · p.31")], now - timedelta(days=5, seconds=-20))

    stack_prompt = "递归调用时，运行栈至少保存哪些信息？为什么返回时能继续执行调用点之后的代码？"
    tree_prompt = "给定二叉树根结点 A，A 的左右孩子为 B、C，B 的左孩子为 D。请写出层次遍历序列，并说明使用什么数据结构。"
    bfs_prompt = "为什么无权图上的 BFS 能得到从起点出发的最少边数路径？"
    for task_id, conversation_id, concept_id, prompt, expected, rubric, created_at in (
        (task_stack, review_id, "demo_stack", stack_prompt, "保存返回地址、参数和局部状态；弹栈后恢复调用现场。", ["说明调用帧保存的状态", "说明返回时如何恢复现场"], now - timedelta(days=1)),
        (task_tree, review_id, "demo_tree_traversal", tree_prompt, "A、B、C、D，使用队列。", ["遍历序列为 A、B、C、D", "明确使用队列"], now - timedelta(hours=18)),
        (task_bfs, assess_id, "demo_bfs", bfs_prompt, "BFS 按距离分层，队列保证先发现较短路径。", ["说明按层扩展", "说明队列与首次访问保证最短步数"], now - timedelta(days=15)),
    ):
        repo.save_trusted_task(_task_data(
            task_id=task_id, project_id=project_id, conversation_id=conversation_id,
            learner_id=learner_id, concept_id=concept_id, ref=refs[concept_id],
            prompt=prompt, expected=expected, rubric=rubric, created_at=created_at,
        ))

    _save_message(repo, review_id, f"msg_{suffix}_r1", "assistant", [_task_card(task_stack, stack_prompt, "栈与递归", source_id, source_title, refs["demo_stack"], "这是刚学习完成的核心知识点，适合用一道短题确认。")], now - timedelta(days=1))
    _save_message(repo, review_id, f"msg_{suffix}_r2", "user", [ContentBlock(type="text", text="会保存返回地址、参数和局部变量。调用结束后弹出当前栈帧，就能恢复上一层现场继续运行。")], now - timedelta(days=1, seconds=-30))
    _save_message(repo, review_id, f"msg_{suffix}_r3", "assistant", [_judgment_card(task_stack, "PASS", "回答覆盖了调用帧中的关键状态，也解释了返回后的现场恢复。", source_id, source_title, refs["demo_stack"]), ContentBlock(type="state_change", data={"mastery_transitions": [{"concept_id": "demo_stack", "old_state": "L0", "new_state": "L1"}]})], now - timedelta(days=1, seconds=-50))
    _save_message(repo, review_id, f"msg_{suffix}_r4", "assistant", [_task_card(task_tree, tree_prompt, "二叉树的层次与遍历", source_id, source_title, refs["demo_tree_traversal"], "这个知识点上次作答不够稳定，安排一次针对练习。")], now - timedelta(hours=18))
    _save_message(repo, review_id, f"msg_{suffix}_r5", "user", [ContentBlock(type="text", text="层次遍历是 A、B、C、D，应该按层依次访问。")], now - timedelta(hours=18, seconds=-25))
    _save_message(repo, review_id, f"msg_{suffix}_r6", "assistant", [_judgment_card(task_tree, "PARTIAL", "遍历顺序正确，但还缺少关键理由：层次遍历使用队列保存下一层待访问结点。", source_id, source_title, refs["demo_tree_traversal"]), ContentBlock(type="state_change", data={"mastery_transitions": [{"concept_id": "demo_tree_traversal", "old_state": "L1", "new_state": "UNSTABLE"}]})], now - timedelta(hours=18, seconds=-45))

    _save_message(repo, assess_id, f"msg_{suffix}_a1", "assistant", [_task_card(task_bfs, bfs_prompt, "图的广度优先搜索", source_id, source_title, refs["demo_bfs"], "这个知识点已经到期，安排一次无提示复验。")], now - timedelta(days=15))
    _save_message(repo, assess_id, f"msg_{suffix}_a2", "user", [ContentBlock(type="text", text="因为队列让距离起点更近的结点先被扩展，结点第一次被访问时经过的边数最少。")], now - timedelta(days=15, seconds=-35))
    _save_message(repo, assess_id, f"msg_{suffix}_a3", "assistant", [_judgment_card(task_bfs, "PASS", "正确说明了按层扩展、队列顺序以及首次访问对应最少边数。", source_id, source_title, refs["demo_bfs"])], now - timedelta(days=15, seconds=-55))
    _save_message(repo, assess_id, f"msg_{suffix}_a4", "assistant", [ContentBlock(type="status", text="本次完成 1 题：通过 1，部分通过 0，未通过 0。学习状态只依据实际作答证据更新。", data={"kind": "consolidation_summary", "total": 1, "counts": {"PASS": 1, "PARTIAL": 0, "FAIL": 0}})], now - timedelta(days=15, seconds=-75))
    return project_id


def reset_and_seed() -> dict:
    _assert_safe_paths()
    connection = sqlite3.connect(DB_PATH)
    try:
        source_row = _find_demo_source(connection)
        users = [row[0] for row in connection.execute("SELECT user_id FROM users ORDER BY created_at")]
        if not users:
            raise RuntimeError("No existing anonymous users were found")
        backup_path = _backup_database(connection)
        removed = _clear_usage_records(connection)
    finally:
        connection.close()

    source_id = source_row["book_id"]
    source_title = source_row["title"]
    by_page = _chunk_lookup(source_id)
    concepts, relations = _build_graph(source_id, by_page)
    refs = {concept.concept_id: concept.source_refs[0] for concept in concepts}

    repo = SqlRepository(f"sqlite:///{DB_PATH.as_posix()}")
    repo.create_schema()
    repo.replace_book_graph(source_id, concepts, relations)
    projects = [
        _seed_project(
            repo, learner_id, source_id, source_title,
            source_row["source_hash"], source_row["original_filename"],
            source_row["parser_version"], refs, index,
        )
        for index, learner_id in enumerate(users, start=1)
    ]
    return {
        "backup": str(backup_path),
        "source_id": source_id,
        "source_title": source_title,
        "source_file_kept": str(PROJECT_ROOT / "data" / "uploads" / source_row["owner_user_id"] / source_id / "source.pdf"),
        "anonymous_users": len(users),
        "demo_projects": len(projects),
        "demo_project_ids": projects,
        "concepts_per_project": len(concepts),
        "conversations_per_project": 3,
        "removed_usage_rows": removed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm replacing local usage records")
    args = parser.parse_args()
    if not args.yes:
        parser.error("This resets local usage records. Re-run with --yes after reviewing the script.")
    result = reset_and_seed()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
