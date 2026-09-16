import { useEffect, useRef, type KeyboardEvent } from "react";
import { useApp, type QueryScope } from "../../store/appStore";
import { MessageBlocks } from "../../components/MessageTimeline";
import { Button } from "../../components/ui/primitives";
import { useConversationActions } from "./useConversationActions";

const DEFAULT_SUGGESTIONS = [
  "帮我概括一下这份资料。",
  "讲讲这一页最重要的概念。",
  "这部分和前面的内容有什么关系？",
  "根据刚读的内容问我一个小问题。",
];

export function ConversationPane({
  heading = "学习助手",
  suggestions = DEFAULT_SUGGESTIONS,
  focused = false,
}: {
  heading?: string;
  suggestions?: string[];
  focused?: boolean;
}) {
  const { state, dispatch } = useApp();
  const actions = useConversationActions();
  const fileRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const activeSource = state.sources.find((source) => source.source_id === state.reader?.sourceId) || state.sources[0];

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [state.messages, state.sending]);

  // Practice is task-led, not a second open-ended chat surface.  A new review
  // conversation stays visually empty until the single practice button has
  // created a task; the composer appears only for an answer or explicit
  // read-only follow-up.
  if (state.mode === "REVIEW" && !state.messages.length && !state.pendingTask) {
    return <section className={`conversation-pane ${focused ? "is-focused" : ""}`} aria-label="等待开始练习" />;
  }

  const onComposerKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (state.activeFollowupTaskId) void actions.followupTask(state.activeFollowupTaskId, state.composer);
      else void actions.send();
    }
  };

  const openCitation = (sourceId: string, page: number) => {
    dispatch({ type: "SET_READER", reader: { sourceId, page } });
  };

  return (
    <section className={`conversation-pane ${focused ? "is-focused" : ""}`}>
      <div className="conversation-pane__head">
        <div><span className="eyebrow">AI 学习伙伴</span><h2>{heading}</h2></div>
        {state.mode === "LEARN" && <ScopePicker sourceTitle={activeSource?.title} page={state.reader?.page} />}
      </div>

      <div ref={scrollRef} className="message-scroll" aria-live="polite">
        {state.messages.length === 0 ? (
          <div className="assistant-empty">
            <div className="assistant-mark">迹</div>
            <h3>{state.mode === "REVIEW" ? "点击左侧按钮开始练习" : "一边看资料，一边把问题聊明白"}</h3>
            <p>{activeSource ? `现在打开的是：${activeSource.title}` : "先从左侧放进一份资料吧，原文件会马上显示。"}</p>
            {state.mode === "LEARN" && <div className="suggestion-grid">
              {suggestions.map((suggestion) => (
                <button key={suggestion} onClick={() => dispatch({ type: "SET_COMPOSER", composer: suggestion })}>{suggestion}<span>↗</span></button>
              ))}
            </div>}
          </div>
        ) : (
          <div className="message-list">
            {state.messages.map((message, index) => (
              <MessageBubble
                key={message.message_id || index}
                role={message.role}
                blocks={message.content_blocks}
                streaming={state.sending && index === state.messages.length - 1 && message.role === "assistant"}
                onCitation={openCitation}
                onTaskSubmit={actions.submitTaskAnswer}
                onTaskHint={actions.requestHint}
                onTaskSkip={actions.skipTask}
                onStartConceptTask={(conceptId) => void actions.startTask({ conceptId })}
                onNextTask={(taskId) => void actions.startTask({ fromTaskId: taskId })}
                onExplainTask={(taskId) => void actions.explainTask(taskId)}
                onPracticeTask={(taskId) => void actions.startTask({ fromTaskId: taskId })}
                onBackToSource={(sourceId, page) => void actions.openTaskSource(sourceId, page)}
                onFinishConsolidation={() => void actions.finishConsolidation()}
                onTaskFollowup={(taskId) => {
                  void actions.startTaskFollowup(taskId).then(() => window.setTimeout(() => document.getElementById("composer")?.focus(), 0));
                }}
                onTaskFollowupEnd={(taskId) => void actions.endTaskFollowup(taskId)}
                onSupplementAnswer={() => {
                  if (state.composer.trim() && !state.sending) {
                    void actions.submitTaskAnswer();
                  } else {
                    window.setTimeout(() => document.getElementById("composer")?.focus(), 0);
                  }
                }}
                hideHint={false}
                activeTaskId={state.pendingTask?.task_id}
                activeFollowupTaskId={state.activeFollowupTaskId}
                canSubmit={Boolean(state.composer.trim()) && !state.sending}
                busy={state.sending}
                onRetry={actions.restoreLastMessage}
              />
            ))}
            {state.sending && state.messages[state.messages.length - 1]?.role === "user" ? (
              <AssistantThinking label={state.pendingTask ? "正在认真判断你的回答" : "正在查找资料并组织回答"} />
            ) : null}
          </div>
        )}
      </div>

      {(state.mode === "LEARN" || state.pendingTask || state.activeFollowupTaskId) && <div className="composer-wrap">
        {state.activeFollowupTaskId ? <div className="selection-context">
          <div><strong>正在追问上一题</strong><span>这段追问不会影响学习状态。结束追问后才能开始下一题。</span></div>
          <button onClick={() => void actions.endTaskFollowup(state.activeFollowupTaskId)}>结束追问</button>
        </div> : null}
        {state.textSelection && state.mode === "LEARN" ? (
          <div className="selection-context">
            <div><strong>已引用第 {state.textSelection.page} 页</strong><span>{state.textSelection.text}</span></div>
            <button onClick={() => dispatch({ type: "SET_TEXT_SELECTION", selection: null })} aria-label="取消引用">×</button>
          </div>
        ) : null}
        <textarea
          id="composer"
          rows={2}
          value={state.composer}
          onChange={(event) => dispatch({ type: "SET_COMPOSER", composer: event.target.value })}
          onKeyDown={onComposerKey}
          disabled={state.sending || !state.activeConversation}
          placeholder={!state.activeConversation ? "正在为这个板块准备一段新对话…" : state.sending ? "我正在想一想…" : state.activeFollowupTaskId ? "追问这道题；这不会影响学习状态" : "把你的答案写在这里"}
          aria-label="消息输入框"
        />
        <div className="composer-actions">
          <span>Shift + Enter 换行</span>
          {state.sending ? <Button onClick={actions.stopGeneration}>停止</Button> : <Button variant="primary" onClick={() => state.activeFollowupTaskId ? void actions.followupTask(state.activeFollowupTaskId, state.composer) : void actions.send()} disabled={!state.composer.trim() || !state.activeConversation}>发送</Button>}
        </div>
      </div>
      }

      <input id="learning-source-upload" ref={fileRef} type="file" accept="application/pdf,.pdf" hidden onChange={(event) => {
        const file = event.target.files?.[0];
        if (file) void actions.uploadFile(file);
        event.target.value = "";
      }} />
    </section>
  );
}

function ScopePicker({ sourceTitle, page }: { sourceTitle?: string; page?: number }) {
  const { state, dispatch } = useApp();
  const options: { value: QueryScope; label: string }[] = [
    { value: "CURRENT_PAGE", label: page ? `当前页 · ${page}` : "当前页" },
    { value: "CURRENT_SOURCE", label: "当前资料" },
    { value: "ALL_SOURCES", label: "全部资料" },
  ];
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <label className="scope-picker" title={sourceTitle || "全部资料"}>
        <span>提问范围</span>
        <select value={state.queryScope} onChange={(event) => dispatch({ type: "SET_QUERY_SCOPE", scope: event.target.value as QueryScope })} disabled={!sourceTitle}>
          {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </label>
      <label title="关闭后，本次提问不会写入知识点疑问记录" style={{ fontSize: 12, color: "var(--muted)", display: "flex", gap: 4 }}>
        <input type="checkbox" checked={state.recordQuestionSignal} onChange={(event) => dispatch({ type: "SET_RECORD_QUESTION_SIGNAL", enabled: event.target.checked })} />
        记录知识点疑问
      </label>
    </div>
  );
}

function MessageBubble({ role, blocks, streaming, onCitation, onTaskSubmit, onTaskHint, onTaskSkip, onStartConceptTask, onNextTask, onExplainTask, onPracticeTask, onBackToSource, onFinishConsolidation, onSupplementAnswer, onTaskFollowup, onTaskFollowupEnd, hideHint, activeTaskId, activeFollowupTaskId, canSubmit, busy, onRetry }: {
  role: "user" | "assistant";
  blocks: import("../../types/blocks").ContentBlock[];
  streaming: boolean;
  onCitation: (sourceId: string, page: number) => void;
  onTaskSubmit: () => void;
  onTaskHint: (taskId: string) => void;
  onTaskSkip: (taskId: string) => void;
  onStartConceptTask: (conceptId: string) => void;
  onNextTask: (taskId: string) => void;
  onExplainTask: (taskId: string) => void;
  onPracticeTask: (taskId: string) => void;
  onBackToSource: (sourceId: string, page: number) => void;
  onFinishConsolidation: () => void;
  onSupplementAnswer: () => void;
  onTaskFollowup: (taskId: string) => void;
  onTaskFollowupEnd: (taskId: string) => void;
  hideHint?: boolean;
  activeTaskId?: string;
  activeFollowupTaskId?: string;
  canSubmit: boolean;
  busy: boolean;
  onRetry: () => void;
}) {
  return (
    <div className={`message-row ${role === "user" ? "is-user" : "is-assistant"}`}>
      {role === "assistant" && <div className="message-avatar">迹</div>}
      <div className="message-content">
        <MessageBlocks blocks={blocks} onCitation={onCitation} onTaskSubmit={onTaskSubmit} onTaskHint={onTaskHint} onTaskSkip={onTaskSkip} onStartConceptTask={onStartConceptTask} onNextTask={onNextTask} onExplainTask={onExplainTask} onPracticeTask={onPracticeTask} onBackToSource={onBackToSource} onFinishConsolidation={onFinishConsolidation} onSupplementAnswer={onSupplementAnswer} onTaskFollowup={onTaskFollowup} onTaskFollowupEnd={onTaskFollowupEnd} hideHint={hideHint} activeTaskId={activeTaskId} activeFollowupTaskId={activeFollowupTaskId} canSubmit={canSubmit} busy={busy} onRetry={onRetry} />
        {streaming && <ThinkingIndicator label="正在整理结果" />}
      </div>
    </div>
  );
}

function AssistantThinking({ label }: { label: string }) {
  return (
    <div className="message-row is-assistant" role="status" aria-live="polite">
      <div className="message-avatar">迹</div>
      <div className="message-content"><ThinkingIndicator label={label} /></div>
    </div>
  );
}

function ThinkingIndicator({ label }: { label: string }) {
  return (
    <div className="thinking-indicator">
      <span className="thinking-spinner" aria-hidden="true" />
      <span>{label}…</span>
    </div>
  );
}
