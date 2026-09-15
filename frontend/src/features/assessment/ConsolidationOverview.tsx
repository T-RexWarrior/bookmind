import { useEffect, useState } from "react";
import * as api from "../../api/client";
import { toast } from "../../components/ui/primitives";
import { useApp } from "../../store/appStore";
import type { ConsolidationFilter, ConsolidationMode, ConsolidationQueue } from "../../types/blocks";
import { useConversationActions } from "../conversations/useConversationActions";

const FILTERS: { value: ConsolidationFilter; label: string }[] = [
  { value: "RECOMMENDED", label: "推荐" },
  { value: "QUESTIONED", label: "问过的" },
  { value: "WEAK", label: "薄弱" },
  { value: "DUE", label: "到期" },
  { value: "UNVERIFIED", label: "未验证" },
  { value: "ALL", label: "全部" },
];

export function ConsolidationOverview() {
  const { state } = useApp();
  const actions = useConversationActions();
  const [filter, setFilter] = useState<ConsolidationFilter>("RECOMMENDED");
  const [queue, setQueue] = useState<ConsolidationQueue | null>(null);
  const [loading, setLoading] = useState(false);
  const projectId = state.activeProject?.project_id;
  const mode: ConsolidationMode = state.mode === "ASSESSMENT" ? "ASSESSMENT" : "PRACTICE";
  const isAssessment = mode === "ASSESSMENT";

  useEffect(() => {
    if (!projectId) return;
    let active = true;
    setLoading(true);
    api.consolidationCandidates(projectId, mode, filter)
      .then((result) => { if (active) setQueue(result); })
      .catch((error) => { if (active) toast((error as Error).message || "暂时无法加载候选知识点"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [projectId, filter, mode, state.summary]);

  const first = queue?.candidates[0];
  return (
    <aside className="activity-overview consolidation-overview review-overview">
      <span className="overview-icon">↻</span>
      <span className="eyebrow">{isAssessment ? "ASSESSMENT" : "PRACTICE"}</span>
      <h2>{isAssessment ? <>独立完成，<br />核对真实掌握</> : <>把疑问变成<br />能够独立作答</>}</h2>
      <p>{isAssessment ? "评估题会单独保存在能力评估会话中。提交后才会依据作答证据更新学习状态。" : "做题、纠错和复验都在这里完成。可以提示或跳过；只有未使用提示并独立答对时，才会形成掌握证据。"}</p>

      <div className="overview-metrics">
        <div><strong>{queue?.counts.questioned || 0}</strong><span>有过疑问</span></div>
        <div><strong>{queue?.counts.weak || 0}</strong><span>测验薄弱</span></div>
        <div><strong>{queue?.counts.due || 0}</strong><span>待复验</span></div>
      </div>

      <button
        className="btn primary consolidation-primary"
        disabled={loading || state.sending || !first}
        onClick={() => void actions.startTask({ selection: filter, mode })}
      >
        {state.sending ? "正在准备题目…" : isAssessment ? "开始能力评估" : "开始推荐练习"}
      </button>

      <div className="candidate-filters" aria-label="筛选知识点">
        {FILTERS.map((item) => (
          <button key={item.value} className={filter === item.value ? "is-active" : ""} onClick={() => setFilter(item.value)}>
            {item.label}
          </button>
        ))}
      </div>

      <div className="focus-list candidate-list">
        <span>选择知识点</span>
        {loading ? <p>正在整理候选知识点…</p> : queue?.candidates.slice(0, 8).map((candidate) => (
          <button
            key={candidate.concept_id}
            disabled={state.sending}
            onClick={() => void actions.startTask({ conceptId: candidate.concept_id, mode })}
          >
            <span><strong>{candidate.name}</strong><small>{candidate.source_title} · {candidate.locator}</small></span>
            <em>{candidate.reason_label}</em>
          </button>
        ))}
        {!loading && !queue?.candidates.length ? <p>这个筛选下暂时没有知识点。可以切换“推荐”或“全部”。</p> : null}
      </div>
    </aside>
  );
}
