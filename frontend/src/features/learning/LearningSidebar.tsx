import { useEffect, useMemo, useState, type ReactNode } from "react";
import * as api from "../../api/client";
import { toast } from "../../components/ui/primitives";
import { useApp } from "../../store/appStore";
import type { ConceptLearningRecord, MisconceptionView } from "../../types/blocks";
import { loadActivityConversation } from "../assessment/ModeSwitcher";
import { useConversationActions } from "../conversations/useConversationActions";
import { KnowledgeGraphPanel } from "./KnowledgeGraphPanel";

const GROUP_LABELS: Record<string, string> = {
  verified: "已验证",
  pending: "待验证",
  weak: "测验薄弱",
  due: "待复验",
};
type RecordFilter = "all" | "questioned" | "verified" | "pending" | "weak" | "due";

export function LearningSidebar() {
  const { state, dispatch } = useApp();
  const actions = useConversationActions();
  const archiveDemo = new URLSearchParams(window.location.search).get("demo") === "binary-tree-archive";
  const summary = state.summary;
  const activeMis = state.misconceptions.filter((item) => item.status !== "DISMISSED");
  const [filter, setFilter] = useState<RecordFilter>("all");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [record, setRecord] = useState<ConceptLearningRecord | null>(null);
  const [loadingRecord, setLoadingRecord] = useState(false);
  const projectId = state.activeProject?.project_id;

  // The drawer can be opened long after an answer was submitted, or after the
  // backend was restarted.  Refresh its read model on every open instead of
  // silently retaining the initial null summary as “尚无学习记录”.
  useEffect(() => {
    if (archiveDemo || !projectId || !state.rightDrawerOpen) return;
    let active = true;
    Promise.all([
      api.learningSummary(projectId),
      api.listMisconceptions(projectId),
    ]).then(([summary, misconceptions]) => {
      if (!active) return;
      dispatch({ type: "SET_SUMMARY", summary, projectId });
      dispatch({ type: "SET_MISCONCEPTIONS", misconceptions, projectId });
    }).catch((error) => {
      if (active) toast((error as Error).message || "学习状态暂时无法刷新");
    });
    return () => { active = false; };
  }, [projectId, state.rightDrawerOpen, dispatch]);

  const concepts = useMemo(() => (summary?.concepts || []).filter((concept) => {
    const matchesFilter = filter === "all"
      || (filter === "questioned" ? (concept.question_count || 0) > 0 : concept.group === filter);
    const needle = query.trim().toLocaleLowerCase();
    const matchesQuery = !needle || [concept.name, concept.chapter, concept.section]
      .filter(Boolean).some((value) => String(value).toLocaleLowerCase().includes(needle));
    return matchesFilter && matchesQuery;
  }).sort((a, b) =>
    `${a.book_id || ""}/${a.chapter || ""}/${a.section || ""}/${a.name}`.localeCompare(
      `${b.book_id || ""}/${b.chapter || ""}/${b.section || ""}/${b.name}`,
      "zh-CN",
      { numeric: true, sensitivity: "base" },
    )
  ), [summary, filter, query]);

  const conceptGroups = useMemo(() => {
    const groups = new Map<string, typeof concepts>();
    for (const concept of concepts) {
      const label = concept.chapter || "未标注章节";
      groups.set(label, [...(groups.get(label) || []), concept]);
    }
    return [...groups.entries()];
  }, [concepts]);

  useEffect(() => {
    if (!projectId || !selectedId) {
      setRecord(null);
      return;
    }
    let active = true;
    setLoadingRecord(true);
    api.conceptLearningRecord(projectId, selectedId)
      .then((value) => { if (active) setRecord(value); })
      .catch((error) => { if (active) toast((error as Error).message || "暂时无法打开知识点记录"); })
      .finally(() => { if (active) setLoadingRecord(false); });
    return () => { active = false; };
  }, [projectId, selectedId, summary]);

  if (archiveDemo) return <BinaryTreeArchiveDemo />;

  async function openSource(sourceId: string, page: number) {
    if (!projectId) return;
    dispatch({ type: "SET_READER", reader: { sourceId, page } });
    dispatch({ type: "SET_DRAWER", right: false });
    if (state.mode === "LEARN") return;
    dispatch({ type: "SET_MODE", mode: "LEARN" });
    try {
      await Promise.all([
        api.updateProject(projectId, { default_mode: "Deep Learning", last_source_id: sourceId, last_source_page: page }),
        loadActivityConversation(projectId, "LEARN", dispatch),
      ]);
    } catch (error) {
      toast((error as Error).message || "暂时无法回到原文");
    }
  }

  return (
    <div>
      <span className="eyebrow">学习档案</span>
      <h2 className="learning-record-title">知识范围与证据</h2>
      {!summary ? <div className="c-muted learning-record-empty">尚无学习记录</div> : (
        <>
          <div className="c-muted learning-record-total">共 {summary.total_concepts} 个知识点；提问不直接等于掌握或薄弱</div>
          <div className="record-filters">
            <FilterButton active={filter === "pending"} onClick={() => setFilter("pending")}>待验证 {summary.groups.pending || 0}</FilterButton>
            <FilterButton active={filter === "questioned"} onClick={() => setFilter("questioned")}>有过疑问 {summary.questioned_count || 0}</FilterButton>
            <FilterButton active={filter === "verified"} onClick={() => setFilter("verified")}>已验证 {summary.groups.verified || 0}</FilterButton>
            <FilterButton active={filter === "weak"} onClick={() => setFilter("weak")}>测验薄弱 {summary.groups.weak || 0}</FilterButton>
            <FilterButton active={filter === "due"} onClick={() => setFilter("due")}>待复验 {summary.groups.due || 0}</FilterButton>
            <FilterButton active={filter === "all"} onClick={() => setFilter("all")}>全部</FilterButton>
          </div>
          <input
            className="record-search"
            aria-label="搜索知识点"
            placeholder="搜索知识点或章节"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <div className="record-concept-list">
            {conceptGroups.map(([chapter, chapterConcepts]) => (
              <details key={chapter} open>
                <summary>{chapter} <small>{chapterConcepts.length} 个知识点</small></summary>
                {chapterConcepts.map((concept) => (
                  <button key={concept.concept_id} className={selectedId === concept.concept_id ? "is-active" : ""} onClick={() => setSelectedId(concept.concept_id)}>
                    <span>
                      <strong>{concept.name}</strong>
                      <small>{concept.profile_summary || concept.section || (concept.manual_learned ? "已学（待验证）" : (GROUP_LABELS[concept.group] || concept.group))}</small>
                    </span>
                    {(concept.attempt_count || 0) > 0
                      ? <em>作答 {concept.attempt_count} 次</em>
                      : (concept.question_count || 0) > 0 ? <em>问过 {concept.question_count} 次</em> : <span>›</span>}
                  </button>
                ))}
              </details>
            ))}
            {!concepts.length ? <p>这个筛选下暂时没有知识点。</p> : null}
          </div>
        </>
      )}

      {selectedId && (
        <section className="concept-record-detail">
          <div className="concept-record-head"><strong>知识点详情</strong><button onClick={() => setSelectedId(null)}>关闭</button></div>
          {loadingRecord ? <p>正在读取证据记录…</p> : record ? (
            <>
              <h3>{record.name}</h3>
              {record.description ? <p>{record.description}</p> : null}
              <div className="concept-record-stats">
                <span>{GROUP_LABELS[record.status.group] || record.status.group}</span>
                <span>当前 {record.status.current_level}</span>
                {record.status.manual_learned ? <span>已学（待验证）</span> : null}
                <span>提问 {record.question_count} 次</span>
                <span>作答 {record.attempt_count} 次</span>
              </div>
              <div className="concept-record-actions">
                <button className="btn primary" onClick={() => void actions.startTask({ conceptId: record.concept_id })}>练习这个知识点</button>
                {record.source_refs[0] ? <button className="btn ghost" onClick={() => void openSource(record.source_refs[0].source_id, record.source_refs[0].page)}>回到原文</button> : null}
              </div>
              {record.learner_profile ? <ProfilePanel profile={record.learner_profile} /> : null}
              <div className="concept-timeline">
                <strong>最近记录</strong>
                {record.timeline.slice(0, 8).map((item) => (
                  <div key={item.evidence_id}>
                    <span>{evidenceLabel(item.type, item.result)}</span>
                    <time>{formatRecordTime(item.occurred_at)}</time>
                    {item.question ? <p>“{item.question}”</p> : null}
                  </div>
                ))}
                {!record.timeline.length ? <p>还没有提问或作答记录。</p> : null}
              </div>
            </>
          ) : null}
        </section>
      )}

      {activeMis.length > 0 && <div className="learning-side-section"><h3>可能的理解偏差</h3>{activeMis.map((item) => <MisconceptionRow key={item.item_id} m={item} />)}</div>}
      {state.activeProject && <div className="learning-side-section"><KnowledgeGraphPanel projectId={state.activeProject.project_id} refreshKey={summary?.total_concepts ?? 0} /></div>}
      <div className="learning-side-section">
        <h3>已加入资料</h3>
        {!state.sources.length ? <div className="c-muted learning-record-empty">尚未添加资料</div> : state.sources.map((source) => (
          <button key={source.source_id} className="record-source-row" onClick={() => void openSource(source.source_id, 1)}>
            {source.state === "SUCCEEDED" ? "✓" : source.state === "PENDING" || source.state === "RUNNING" ? "⏳" : "⚠"} {source.title || "学习资料"}
          </button>
        ))}
      </div>
    </div>
  );
}

function FilterButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return <button className={active ? "is-active" : ""} onClick={onClick}>{children}</button>;
}

function evidenceLabel(type: string, result?: string | null): string {
  if (type === "QUESTION") return "提出疑问";
  if (type === "READ") return "阅读接触";
  if (type === "HINT") return "查看提示（辅助）";
  if (type === "SKIP") return "跳过本题";
  if (type === "EXPLANATION") return "查看讲解（辅助）";
  const resultLabel = result === "PASS" ? "通过" : result === "PARTIAL" ? "部分通过" : result === "FAIL" ? "未通过" : "已记录";
  return `${type === "PROBE" ? "诊断题" : type === "CHANGED_TASK" ? "迁移题" : "检测题"} · ${resultLabel}`;
}

/** Presentation-only read model. It demonstrates the evidence-led archive
 * without mixing the learner's real project history into a PPT screenshot. */
function BinaryTreeArchiveDemo() {
  return <div className="archive-demo">
    <span className="eyebrow">学习档案</span>
    <h2 className="learning-record-title">知识范围与证据</h2>
    <p className="c-muted learning-record-total">聚焦 1 个知识点 · 2 次独立作答 · 记录事实而非只记录分数</p>

    <section className="archive-demo-overview">
      <span className="chip pending">当前 L1</span>
      <strong>§5.1 二叉树及其表示</strong>
      <p>已具备结点表示的独立作答证据；本次在“表示 → 遍历应用”上部分通过，下一步需要补足递归边界与访问顺序。</p>
      <div><span>已通过 1</span><span>部分通过 1</span><span>待复验 1</span></div>
    </section>

    <section className="learner-profile-panel archive-demo-profile">
      <strong>基于证据的学习画像</strong>
      <p>学习者能说明 left/right 分别连接左右子树，并能用 None 表示空孩子；但尚未稳定说明空树基线及前序遍历的根→左→右访问时机。</p>
      <ProfileList label="已观察到" items={["理解二叉树结点的左右孩子引用", "知道空孩子可用 None / 空指针表示"]} />
      <ProfileList label="待关注" tone="attention" items={["空树与单结点的递归终止条件", "前序遍历的根→左→右访问顺序"]} />
      <div className="profile-goal"><span>下一步验证</span>针对空树、单结点、仅有左孩子三种边界情境，手写一次前序遍历递归过程。</div>
      <small>依据：2 次独立作答的题干、作答事实与判分要点。已验证状态不因一次部分正确被简单覆盖。</small>
    </section>

    <section className="concept-timeline archive-demo-timeline">
      <strong>证据时间线</strong>
      <article className="archive-demo-event partial">
        <div><span>推荐练习 · 部分通过</span><time>刚刚</time></div>
        <p>已覆盖：left/right 引用、空孩子表示、递归访问子树。</p>
        <p>待补足：空树终止条件，以及前序遍历“根→左→右”的访问时机。</p>
        <small>独立作答 · 未查看提示 · 已追加至证据链</small>
      </article>
      <article className="archive-demo-event pass">
        <div><span>学习检测 · 通过</span><time>此前</time></div>
        <p>能说明结点表示、左右孩子引用与空孩子表示如何支持后续操作。</p>
        <small>独立作答 · L0 → L1 · 保留为既有通过证据</small>
      </article>
    </section>

    <section className="archive-demo-boundary">
      <strong>学习状态为何保持 L1？</strong>
      <p>学习档案保留每一次独立作答的事实。一次部分正确会形成新的补救目标，但不会抹去先前已经获得的通过证据；是否升级仍需后续独立验证。</p>
    </section>
  </div>;
}

function ProfilePanel({ profile }: { profile: NonNullable<ConceptLearningRecord["learner_profile"]> }) {
  const hasContent = profile.summary || profile.observed_understanding?.length
    || profile.needs_attention?.length || profile.next_practice_goal;
  if (!hasContent) return null;
  return <section className="learner-profile-panel">
    <strong>基于证据的学习画像</strong>
    {profile.summary ? <p>{profile.summary}</p> : null}
    {profile.observed_understanding?.length ? <ProfileList label="已观察到" items={profile.observed_understanding} /> : null}
    {profile.needs_attention?.length ? <ProfileList label="待关注" items={profile.needs_attention} tone="attention" /> : null}
    {profile.next_practice_goal ? <div className="profile-goal"><span>下一步验证</span>{profile.next_practice_goal}</div> : null}
    {profile.evidence_basis?.length ? <small>依据：{profile.evidence_basis.join("；")}</small> : null}
    <small className="profile-disclaimer">由模型解释已有学习事实；“已验证”仍仅来自独立作答。</small>
  </section>;
}

function ProfileList({ label, items, tone = "" }: { label: string; items: string[]; tone?: string }) {
  return <div className={`profile-list ${tone}`}><span>{label}</span><ul>{items.map((item) => <li key={item}>{item}</li>)}</ul></div>;
}

function formatRecordTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

function MisconceptionRow({ m }: { m: MisconceptionView }) {
  return <div className="misconception-row"><span className="chip weak">{m.status_label || m.status}</span><span>{m.changed_task_pass_count ? `复验 ${m.changed_task_pass_count}/2` : "待确认"}</span></div>;
}
