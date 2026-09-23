import { useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../../store/appStore";
import { useConversationActions } from "../conversations/useConversationActions";

export function ConsolidationOverview() {
  const { state, dispatch } = useApp();
  const actions = useConversationActions();
  const [entryMode, setEntryMode] = useState<"RECOMMENDED" | "CONCEPT">("RECOMMENDED");
  const clearedDemoConversationRef = useRef("");
  const demoMode = new URLSearchParams(window.location.search).get("demo");
  const presentationDemo = demoMode === "practice-binary-tree" || demoMode === "binary-tree-archive";

  useEffect(() => {
    if (!presentationDemo || !state.activeConversation || !state.messages.length) return;
    // A review conversation is durable, while the PPT fixture is deliberately
    // in-memory.  The activity loader can finish after this component mounts
    // and repopulate an old real chat.  Clear that historical payload once;
    // never clear messages created by the fixture itself.
    const hasFixtureMessage = state.messages.some((message) => message.message_id.startsWith("demo_"));
    const conversationId = state.activeConversation.conversation_id;
    if (hasFixtureMessage || clearedDemoConversationRef.current === conversationId) return;
    clearedDemoConversationRef.current = conversationId;
    dispatch({ type: "SET_MESSAGES", messages: [] });
    dispatch({ type: "SET_PENDING_TASK", pendingTask: null });
    dispatch({ type: "SET_FOLLOWUP_TASK", taskId: "" });
  }, [presentationDemo, state.activeConversation, state.messages, dispatch]);
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
      <p>系统会结合学习档案推荐下一题，也可以指定一个知识点出题。只有实际作答会影响学习状态。</p>

      <div className="candidate-filters" aria-label="出题方式">
        <button className={entryMode === "RECOMMENDED" ? "is-active" : ""} onClick={() => setEntryMode("RECOMMENDED")}>推荐练习</button>
        <button className={entryMode === "CONCEPT" ? "is-active" : ""} onClick={() => setEntryMode("CONCEPT")}>按知识点出题</button>
      </div>

      {entryMode === "RECOMMENDED" ? (
        <>
          <div className="recommendation-rationale">
            <strong>推荐依据</strong>
            <span>① 待复习或尚未稳定的知识点</span>
            <span>② 已学但尚未独立验证的知识点</span>
            <span>③ 刚通过当前层级、适合继续确认的知识点</span>
          </div>
          <button
            className="btn primary consolidation-primary"
            disabled={state.sending || Boolean(state.activeFollowupTaskId) || !concepts.some((concept) => concept.level !== "L4")}
            onClick={() => void actions.startTask({ selection: "RECOMMENDED" })}
          >
            {state.sending ? "正在分析学习档案…" : "开始一道推荐练习题"}
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
