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
  const summary = state.summary;
  const activeMis = state.misconceptions.filter((item) => item.status !== "DISMISSED");
  const [filter, setFilter] = useState<RecordFilter>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [record, setRecord] = useState<ConceptLearningRecord | null>(null);
  const [loadingRecord, setLoadingRecord] = useState(false);
  const projectId = state.activeProject?.project_id;

  const concepts = useMemo(() => (summary?.concepts || []).filter((concept) => {
    if (filter === "all") return true;
    if (filter === "questioned") return (concept.question_count || 0) > 0;
    return concept.group === filter;
  }), [summary, filter]);

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
          <div className="record-concept-list">
            {concepts.slice(0, 40).map((concept) => (
              <button key={concept.concept_id} className={selectedId === concept.concept_id ? "is-active" : ""} onClick={() => setSelectedId(concept.concept_id)}>
                <span><strong>{concept.name}</strong><small>{GROUP_LABELS[concept.group] || concept.group}</small></span>
                {(concept.question_count || 0) > 0 ? <em>问过 {concept.question_count} 次</em> : <span>›</span>}
              </button>
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
                <span>提问 {record.question_count} 次</span>
                <span>作答 {record.attempt_count} 次</span>
              </div>
              <div className="concept-record-actions">
                <button className="btn primary" onClick={() => void actions.startTask({ conceptId: record.concept_id })}>练习这个知识点</button>
                {record.source_refs[0] ? <button className="btn ghost" onClick={() => void openSource(record.source_refs[0].source_id, record.source_refs[0].page)}>回到原文</button> : null}
              </div>
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
  const resultLabel = result === "PASS" ? "通过" : result === "PARTIAL" ? "部分通过" : result === "FAIL" ? "未通过" : "已记录";
  return `${type === "PROBE" ? "诊断题" : type === "CHANGED_TASK" ? "迁移题" : "检测题"} · ${resultLabel}`;
}

function formatRecordTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

function MisconceptionRow({ m }: { m: MisconceptionView }) {
  return <div className="misconception-row"><span className="chip weak">{m.status_label || m.status}</span><span>{m.changed_task_pass_count ? `复验 ${m.changed_task_pass_count}/2` : "待确认"}</span></div>;
}
