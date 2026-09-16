import { useMemo, useState } from "react";
import { useApp } from "../../store/appStore";
import { useConversationActions } from "../conversations/useConversationActions";

export function ConsolidationOverview() {
  const { state } = useApp();
  const actions = useConversationActions();
  const [entryMode, setEntryMode] = useState<"RANDOM" | "CONCEPT">("RANDOM");
  const concepts = useMemo(
    () => [...(state.summary?.concepts || [])].sort((a, b) => {
      // Put unverified concepts first, while still allowing a learner to
      // deliberately reopen an already-passed concept for review.
      const aPassed = a.level === "L4" ? 1 : 0;
      const bPassed = b.level === "L4" ? 1 : 0;
      return aPassed - bPassed || a.name.localeCompare(b.name, "zh-CN");
    }),
    [state.summary],
  );
  return (
    <aside className="activity-overview consolidation-overview review-overview">
      <span className="overview-icon">↻</span>
      <span className="eyebrow">PRACTICE</span>
      <h2>练习巩固</h2>
      <p>可以随机练习，也可以指定一个知识点出题。只有实际作答会影响学习状态。</p>

      <div className="candidate-filters" aria-label="出题方式">
        <button className={entryMode === "RANDOM" ? "is-active" : ""} onClick={() => setEntryMode("RANDOM")}>随机出题</button>
        <button className={entryMode === "CONCEPT" ? "is-active" : ""} onClick={() => setEntryMode("CONCEPT")}>按知识点出题</button>
      </div>

      {entryMode === "RANDOM" ? (
        <>
          <p className="c-muted" style={{ fontSize: 12 }}>随机从尚未达到 L4 的知识点中选择。</p>
          <button
            className="btn primary consolidation-primary"
            disabled={state.sending || Boolean(state.activeFollowupTaskId) || !concepts.some((concept) => concept.level !== "L4")}
            onClick={() => void actions.startTask({ selection: "RANDOM" })}
          >
            {state.sending ? "正在准备题目…" : "开始一道随机练习题"}
          </button>
        </>
      ) : (
        <div className="focus-list candidate-list" aria-label="按知识点出题">
          <span>选择知识点</span>
          {concepts.map((concept) => (
            <button
              key={concept.concept_id}
              disabled={state.sending || Boolean(state.activeFollowupTaskId)}
              onClick={() => void actions.startTask({ conceptId: concept.concept_id })}
            >
              <span><strong>{concept.name}</strong><small>{concept.level === "L4" ? "已通过，可复验" : `当前 ${concept.level}，待验证`}</small></span>
              <em>出题</em>
            </button>
          ))}
          {!concepts.length ? <p>资料解析完成后，这里会显示可选知识点。</p> : null}
        </div>
      )}
      {state.activeFollowupTaskId ? <p className="c-muted" style={{ fontSize: 12 }}>请先在右侧结束上一题追问，才能开始新题。</p> : null}
    </aside>
  );
}
