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
  const activeSource = state.sources.find((source) => source.source_id === state.reader?.sourceId);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [state.messages, state.sending]);

  const onComposerKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void actions.send();
    }
  };

  const openCitation = (sourceId: string, page: number) => {
    dispatch({ type: "SET_READER", reader: { sourceId, page } });
  };

  return (
    <section className={`conversation-pane ${focused ? "is-focused" : ""}`}>
      <div className="conversation-pane__head">
        <div><span className="eyebrow">AI 学习伙伴</span><h2>{heading}</h2></div>
        <ScopePicker sourceTitle={activeSource?.title} page={state.reader?.page} />
      </div>

      {state.mode !== "LEARN" && !state.pendingTask && (
        <div className="assessment-start-card">
          <div>
            <strong>练习一个知识点</strong>
            <span>可以请求提示、查看解释或跳过；未使用提示并独立答对时，才会形成掌握证据。</span>
          </div>
          <Button
            variant="primary"
            onClick={() => void actions.startTask({})}
            disabled={state.sending || !state.activeConversation}
          >
            {state.sending ? "正在出题…" : "开始一道练习题"}
          </Button>
        </div>
      )}

      <div ref={scrollRef} className="message-scroll" aria-live="polite">
        {state.messages.length === 0 ? (
          <div className="assistant-empty">
            <div className="assistant-mark">迹</div>
            <h3>{state.mode === "REVIEW" ? "今天想巩固哪个知识点？" : "一边看资料，一边把问题聊明白"}</h3>
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
                onNextTask={() => void actions.startTask({})}
                onExplainTask={(taskId) => void actions.explainTask(taskId)}
                onPracticeTask={(taskId) => void actions.startTask({ fromTaskId: taskId })}
                onBackToSource={(sourceId, page) => void actions.openTaskSource(sourceId, page)}
                onFinishConsolidation={() => void actions.finishConsolidation()}
                onSupplementAnswer={() => window.setTimeout(() => document.getElementById("composer")?.focus(), 0)}
                hideHint={false}
                activeTaskId={state.pendingTask?.task_id}
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

      <div className="composer-wrap">
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
          placeholder={!state.activeConversation ? "正在为这个板块准备一段新对话…" : state.sending ? "我正在想一想…" : state.pendingTask ? "把你的答案写在这里" : "有哪里没看懂？直接问就好，Enter 发送"}
          aria-label="消息输入框"
        />
        <div className="composer-actions">
          <span>Shift + Enter 换行</span>
          {state.sending ? <Button onClick={actions.stopGeneration}>停止</Button> : <Button variant="primary" onClick={actions.send} disabled={!state.composer.trim() || !state.activeConversation}>发送</Button>}
        </div>
      </div>

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
    <label className="scope-picker" title={sourceTitle || "全部资料"}>
      <span>提问范围</span>
      <select value={state.queryScope} onChange={(event) => dispatch({ type: "SET_QUERY_SCOPE", scope: event.target.value as QueryScope })} disabled={!sourceTitle}>
        {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
    </label>
  );
}

function MessageBubble({ role, blocks, streaming, onCitation, onTaskSubmit, onTaskHint, onTaskSkip, onStartConceptTask, onNextTask, onExplainTask, onPracticeTask, onBackToSource, onFinishConsolidation, onSupplementAnswer, hideHint, activeTaskId, canSubmit, busy, onRetry }: {
  role: "user" | "assistant";
  blocks: import("../../types/blocks").ContentBlock[];
  streaming: boolean;
  onCitation: (sourceId: string, page: number) => void;
  onTaskSubmit: () => void;
  onTaskHint: (taskId: string) => void;
  onTaskSkip: (taskId: string) => void;
  onStartConceptTask: (conceptId: string) => void;
  onNextTask: () => void;
  onExplainTask: (taskId: string) => void;
  onPracticeTask: (taskId: string) => void;
  onBackToSource: (sourceId: string, page: number) => void;
  onFinishConsolidation: () => void;
  onSupplementAnswer: () => void;
  hideHint?: boolean;
  activeTaskId?: string;
  canSubmit: boolean;
  busy: boolean;
  onRetry: () => void;
}) {
  return (
    <div className={`message-row ${role === "user" ? "is-user" : "is-assistant"}`}>
      {role === "assistant" && <div className="message-avatar">迹</div>}
      <div className="message-content">
        <MessageBlocks blocks={blocks} onCitation={onCitation} onTaskSubmit={onTaskSubmit} onTaskHint={onTaskHint} onTaskSkip={onTaskSkip} onStartConceptTask={onStartConceptTask} onNextTask={onNextTask} onExplainTask={onExplainTask} onPracticeTask={onPracticeTask} onBackToSource={onBackToSource} onFinishConsolidation={onFinishConsolidation} onSupplementAnswer={onSupplementAnswer} hideHint={hideHint} activeTaskId={activeTaskId} canSubmit={canSubmit} busy={busy} onRetry={onRetry} />
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
