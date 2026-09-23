// Structured message cards. These render server-owned payloads and MUST NOT
// display rubric, expected_answer, Concept IDs, bug_ids, or any internal field
// — only the prompt, the kind, and (for judgment cards) the result + reason
// (PRODUCTIZATION §2.4, §5.6, enforced by tests/test_api_m4_closure.py).

import type { ReactNode } from "react";
import type {
  JudgmentCardData,
  TaskCompletionCardData,
  JudgmentPayload,
  QuestionSignalData,
  StateChangeData,
  TaskCardData,
  Transition,
} from "../../types/blocks";
import { MathText } from "../MathText";

export function QuestionSignalCard({
  data,
  onStart,
  busy,
}: {
  data: QuestionSignalData;
  onStart: (conceptId: string) => void;
  busy: boolean;
}) {
  if (!data.concepts?.length) {
    return data.unclassified ? (
      <div className="question-signal-card">
        <div><span className="chip">未强行归类</span><p>{data.message}</p></div>
      </div>
    ) : null;
  }
  return (
    <div className="question-signal-card">
      <div>
        <span className="chip questioned">{data.record_label || "有过疑问 · 待验证"}</span>
        <p>{data.message}</p>
      </div>
      {data.concepts.map((concept) => (
        <div className="question-signal-concept" key={concept.concept_id}>
          <strong>{concept.name}</strong>
          <div className="question-signal-actions">
            <button className="btn primary" disabled={busy} onClick={() => onStart(concept.concept_id)}>练习巩固</button>
          </div>
        </div>
      ))}
    </div>
  );
}

const TASK_KIND_LABEL: Record<string, string> = {
  probe: "区分性小问题",
  changed_task: "换一种情境再试",
  quiz: "学习检测",
  judgment: "判定结果",
};

export function TaskCard({
  data,
  onSubmit,
  onHint,
  onSkip,
  hideHint,
  interactive,
  canSubmit,
  busy,
}: {
  data: TaskCardData;
  onSubmit: () => void;
  onHint: () => void;
  onSkip: () => void;
  hideHint?: boolean;
  interactive: boolean;
  canSubmit: boolean;
  busy: boolean;
}) {
  const label = TASK_KIND_LABEL[data.kind] || "任务";
  const stageBadge =
    data.kind === "changed_task" && data.remediation_stage ? (
      <span className="chip weak" style={{ marginLeft: 8 }}>
        {data.remediation_stage === 1 ? "近迁移" : "远迁移"}
      </span>
    ) : null;
  return (
    <div
      className="r-radius"
      style={{
        maxWidth: 720,
        margin: "12px auto",
        background: "var(--panel)",
        border: "1px solid var(--border)",
        borderLeft: `3px solid ${data.kind === "probe" ? "var(--accent)" : "var(--warn)"}`,
        padding: "16px 20px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        <span className="chip pending">{label}</span>
        {stageBadge}
      </div>
      <div style={{ fontSize: 14, lineHeight: 1.6, marginBottom: 16 }}><MathText text={data.prompt_text} /></div>
      {(data.focus || data.generation_reason || data.generation_notice || data.source_scope?.length) && (
        <div className="task-provenance">
          {data.focus && <div><span>考查内容</span><strong>{data.focus}</strong></div>}
          {data.source_scope?.length ? <div><span>资料依据</span><strong>{data.source_scope.map((item) => `${item.title} · ${item.locator}`).join("；")}</strong></div> : null}
          {data.generation_reason && <div><span>为什么现在问</span><strong>{data.generation_reason}</strong></div>}
          {data.generation_notice && <div><span>{data.generation_mode === "llm" ? "出题方式" : "生成状态"}</span><strong>{data.generation_notice}</strong></div>}
        </div>
      )}
      {interactive ? (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <button className="btn primary" onClick={onSubmit} disabled={!canSubmit || busy} title={!canSubmit ? "请先在下方写下答案" : undefined}>提交答案</button>
          {hideHint ? null : <button className="btn ghost" onClick={onHint} disabled={busy}>提示</button>}
          <button className="btn ghost" onClick={onSkip} disabled={busy}>跳过</button>
          {!canSubmit && !busy ? <span className="task-input-help">请先在下方写下答案</span> : null}
        </div>
      ) : <span className="chip">这道题已结束</span>}
    </div>
  );
}

const JUDGMENT_LABEL: Record<string, string> = {
  PASS: "通过",
  PARTIAL: "部分通过",
  FAIL: "未通过",
  needs_review: "暂无法可靠判断",
};

export function JudgmentCard({
  data,
  onExplain,
  onSupplement,
  onSkip,
  busy,
}: {
  data: JudgmentCardData;
  onExplain: (taskId: string) => void;
  onSupplement: () => void;
  onSkip: (taskId: string) => void;
  busy: boolean;
}) {
  const j: JudgmentPayload = data.judgment || {};
  const result = j.result || (j.judgment_status === "NEEDS_REVIEW" ? "needs_review" : "");
  const label = JUDGMENT_LABEL[result] || "暂无法可靠判断";
  const color =
    result === "PASS" ? "var(--ok)" : result === "FAIL" ? "var(--danger)" : result === "PARTIAL" ? "var(--warn)" : "var(--muted)";
  const criteria = result === "needs_review" ? [] : (j.criterion_results || []).map((c, i) => (
    <li key={i} style={{ color: c.satisfied ? "var(--ok)" : "var(--danger)", margin: "2px 0" }}>
      {c.satisfied ? "✓" : "✗"} {c.satisfied ? `已覆盖要点 ${i + 1}` : `还需补充要点 ${i + 1}`}
    </li>
  ));
  return (
    <div
      className="r-radius"
      style={{
        maxWidth: 720,
        margin: "12px auto",
        background: "var(--panel)",
        border: "1px solid var(--border)",
        padding: "16px 20px",
      }}
    >
      <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6, color }}>{label}</div>
      {j.reason ? <div className="c-muted" style={{ fontSize: 13, marginBottom: 8 }}>{j.reason}</div> : null}
      {criteria.length ? <ul style={{ listStyle: "none", padding: 0, margin: "8px 0 0", fontSize: 13 }}>{criteria}</ul> : null}
      {data.needs_review ? (
        <div className="c-muted" style={{ fontSize: 12, fontStyle: "italic", marginTop: 8 }}>
          原题仍然保留。补充你的结论和理由后可以再次提交，这次不会记为错误答案。
        </div>
      ) : null}
      <div className="judgment-actions">
        {data.needs_review ? (
          <>
            <button className="btn primary" disabled={busy} onClick={onSupplement}>提交补充答案</button>
            <button className="btn" disabled={busy} onClick={() => onExplain(data.task_id)}>查看讲解并结束本题</button>
            <button className="btn ghost" disabled={busy} onClick={() => onSkip(data.task_id)}>跳过本题</button>
          </>
        ) : (
          <span className="c-muted" style={{ fontSize: 12 }}>本题已判定；请在下方选择下一步。</span>
        )}
      </div>
    </div>
  );
}

export function TaskCompletionCard({
  data, onNext, onExplain, onFollowup, onFollowupEnd, onBackToSource, onFinish, followupActive, busy,
}: {
  data: TaskCompletionCardData;
  onNext: (taskId: string) => void;
  onExplain: (taskId: string) => void;
  onFollowup: (taskId: string) => void;
  onFollowupEnd: (taskId: string) => void;
  onBackToSource: (sourceId: string, page: number) => void;
  onFinish: () => void;
  followupActive: boolean;
  busy: boolean;
}) {
  const label = data.completion_status === "EXPLAINED"
    ? "已查看讲解并结束本题"
    : data.completion_status === "SKIPPED" ? "已跳过本题" : "这道题已结束";
  const source = data.source_scope?.[0];
  return <div className="r-radius" style={{ maxWidth: 720, margin: "12px auto", background: "var(--panel)", border: "1px solid var(--border)", padding: "16px 20px" }}>
    <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6 }}>{label}</div>
    {followupActive ? <div className="judgment-actions">
      <span className="c-muted" style={{ fontSize: 12 }}>正在追问本题；结束追问后才可开始下一题。</span>
      <button className="btn primary" disabled={busy} onClick={() => onFollowupEnd(data.task_id)}>结束追问</button>
    </div> : <div className="judgment-actions">
      <button className="btn primary" disabled={busy} onClick={() => onNext(data.task_id)}>下一题</button>
      {data.completion_status !== "EXPLAINED" ? <button className="btn" disabled={busy} onClick={() => onExplain(data.task_id)}>查看讲解</button> : null}
      <button className="btn" disabled={busy} onClick={() => onFollowup(data.task_id)}>追问本题</button>
      {source ? <button className="btn ghost" disabled={busy} onClick={() => onBackToSource(source.source_id, source.page || 1)}>回原文</button> : null}
      <button className="btn ghost" disabled={busy} onClick={onFinish}>结束本次练习</button>
    </div>}
  </div>;
}

export function TaskOptionsCard({
  taskId, onHint, onExplain, onSkip, onSupplement, hideHint, busy,
}: {
  taskId: string;
  onHint: (taskId: string) => void;
  onExplain: (taskId: string) => void;
  onSkip: (taskId: string) => void;
  onSupplement: () => void;
  hideHint?: boolean;
  busy: boolean;
}) {
  return (
    <div className="r-radius" style={{ maxWidth: 720, margin: "12px auto", background: "var(--panel)", border: "1px solid var(--border)", padding: "16px 20px" }}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>这题暂时没有思路很正常。</div>
      <div className="c-muted" style={{ fontSize: 13, marginBottom: 12 }}>请选择下一步；在你明确选择前，题目和学习状态都不会被改动。</div>
      <div className="judgment-actions">
        {hideHint ? null : <button className="btn primary" disabled={busy} onClick={() => onHint(taskId)}>给我提示</button>}
        <button className="btn" disabled={busy} onClick={() => onExplain(taskId)}>查看讲解并结束本题</button>
        <button className="btn" disabled={busy} onClick={onSupplement}>提交答案</button>
        <button className="btn ghost" disabled={busy} onClick={() => onSkip(taskId)}>跳过本题</button>
      </div>
    </div>
  );
}

function renderTransition(t: Transition, kind: "mastery" | "misconception"): ReactNode {
  if (t.old_state === t.new_state) return null;
  return (
    <div key={`${t.concept_id || t.bug_id}-${t.old_state}-${t.new_state}`} style={{ display: "flex", alignItems: "center", gap: 6, margin: "3px 0" }}>
      <span className={`chip ${kind === "mastery" ? "pending" : "weak"}`}>{t.old_state || "—"}</span>
      <span className="c-muted">→</span>
      <span className={`chip ${kind === "mastery" ? "verified" : "due"}`}>{t.new_state || "—"}</span>
    </div>
  );
}

export function StateChangeCard({ data }: { data: StateChangeData }) {
  const items: ReactNode[] = [];
  for (const t of data.misconception_transitions || []) {
    const n = renderTransition(t, "misconception");
    if (n) items.push(n);
  }
  for (const t of data.mastery_transitions || []) {
    const n = renderTransition(t, "mastery");
    if (n) items.push(n);
  }
  if (!items.length) return null;
  return (
    <div
      className="r-radius bg-panel2"
      style={{ maxWidth: 720, margin: "8px auto", fontSize: 13, color: "var(--muted)", padding: "10px 14px" }}
    >
      <div style={{ fontSize: 12, marginBottom: 6 }}>学习状态更新</div>
      {items}
    </div>
  );
}

export function CitationChip({
  label,
  sourceId,
  page,
  onOpen,
}: {
  label: string;
  sourceId: string;
  page: string;
  onOpen: (sourceId: string, page: number) => void;
}) {
  const clickable = !!sourceId;
  return (
    <span
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={clickable ? () => onOpen(sourceId, parseInt(page, 10) || 1) : undefined}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onOpen(sourceId, parseInt(page, 10) || 1);
              }
            }
          : undefined
      }
      style={{
        display: "inline-block",
        background: "var(--panel-2)",
        border: `1px solid ${clickable ? "var(--accent-soft)" : "var(--border)"}`,
        borderRadius: 6,
        padding: "2px 8px",
        fontSize: 12,
        margin: "4px 6px 0 0",
        color: clickable ? "var(--accent)" : "var(--muted)",
        cursor: clickable ? "pointer" : "default",
      }}
    >
      {label}
    </span>
  );
}

export function ContextCard({ data }: { data: Record<string, unknown> }) {
  if (data.kind === "selection_context") {
    return (
      <div className="message-selection-context">
        <strong>引用第 {String(data.page || "—")} 页原文</strong>
        <span>“{String(data.quote || "")}”</span>
      </div>
    );
  }
  const scope = String(data.scope || "全部资料");
  const reason = String(data.reason || "");
  const items = (Array.isArray(data.items) ? data.items : []) as { title?: string; locator?: string }[];
  const learnerBasis = (Array.isArray(data.learner_basis) ? data.learner_basis : []) as { label?: string; text?: string }[];
  return (
    <div className="answer-context">
      <div className="answer-context__head"><span>回答依据</span><strong>{scope}</strong></div>
      {items.length ? <div className="answer-context__items">{items.map((item, index) => <span key={`${item.title}-${index}`}>{item.title || "学习资料"} · {item.locator || "相关内容"}</span>)}</div> : null}
      {reason && <p>{reason}</p>}
      {learnerBasis.length ? <div className="answer-context__basis">
        <strong>学习判断依据</strong>
        {learnerBasis.map((item, index) => <p key={`${item.label}-${index}`}><b>{item.label || "记录"}</b>：{item.text || "—"}</p>)}
      </div> : null}
    </div>
  );
}

export function ErrorRetryCard({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div
      className="r-radius"
      style={{
        maxWidth: 720,
        margin: "12px auto",
        padding: "14px 18px",
        border: "1px solid var(--danger)",
        background: "color-mix(in srgb, var(--danger) 6%, var(--panel))",
        color: "var(--danger)",
        fontSize: 13,
      }}
    >
      <div style={{ marginBottom: onRetry ? 10 : 0 }}>⚠ {message}</div>
      {onRetry ? <button className="btn" onClick={onRetry}>重试</button> : null}
    </div>
  );
}

export function StatusLine({ text }: { text: string }) {
  return <div className="c-muted" style={{ fontSize: 12, fontStyle: "italic", margin: "4px 0" }} aria-live="polite">{text}</div>;
}
