// IngestionCard — the five-stage processing progress (PRODUCTIZATION §5.3).
// Shown while uploading or while a job is in flight. Progress comes from the
// real job stage, never a timer.

import { useApp } from "../../store/appStore";
import { PROCESSING_STAGES, STAGE_LABELS, isTerminalJobState } from "../shared";
import { Button } from "../../components/ui/primitives";
import * as api from "../../api/client";
import { toast } from "../../components/ui/primitives";

export function IngestionCard({ compact = false }: { compact?: boolean }) {
  const { state, dispatch } = useApp();
  const job = state.job;

  if (job && !isTerminalJobState(job.state)) {
    const curIdx = PROCESSING_STAGES.indexOf((job.user_stage as typeof PROCESSING_STAGES[number]) || "upload");
    return (
      <div className={`ingestion-card ${compact ? "is-compact" : ""}`}>
        <div className="ingestion-card__title">正在帮你整理这份资料 <span>{Math.round((job.progress || 0) * 100)}%</span></div>
        {job.pages_total ? (
          <div className="c-muted" style={{ fontSize: 12, marginBottom: 10 }}>
            {job.checkpoint_stage || `解析第 ${job.pages_done}/${job.pages_total} 页`}
            {job.parser_mode ? ` · ${job.parser_mode}` : ""}
          </div>
        ) : null}
        <ol style={{ listStyle: "none", display: "flex", flexDirection: "column", gap: 10, padding: 0 }}>
          {PROCESSING_STAGES.map((k, i) => {
            const st = i < curIdx ? "done" : i === curIdx ? "active" : "wait";
            const color = st === "done" ? "var(--ok)" : st === "active" ? "var(--accent)" : "var(--border)";
            return (
              <li key={k} style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13, color: st === "wait" ? "var(--muted)" : "var(--text)" }}>
                <span style={{ width: 10, height: 10, borderRadius: "50%", background: color, flexShrink: 0 }} />
                {STAGE_LABELS[k]}
              </li>
            );
          })}
        </ol>
        <button
          className="ingestion-cancel"
          onClick={async () => {
            if (!window.confirm("停止整理这份资料？原 PDF 会保留，之后仍可重新处理。")) return;
            try {
              const result = await api.cancelJob(job.job_id);
              dispatch({ type: "SET_JOB", job: { ...job, state: result.state, error: "" }, projectId: state.activeProject?.project_id });
            } catch (error) {
              toast((error as Error).message || "暂时无法停止处理");
            }
          }}
        >停止处理</button>
      </div>
    );
  }

  // P1-08: surface ANY non-success terminal state (FAILED, RETRYABLE_FAILED,
  // CANCELLED) with the server's error reason and a retry button. The runner
  // commonly uses RETRYABLE_FAILED (compressed/scanned/encrypted), which the
  // old code ignored (it only matched "FAILED").
  const failedState = job && ["FAILED", "RETRYABLE_FAILED", "CANCELLED"].includes(job.state) ? job.state : null;
  if (job && failedState) {
    const cancelled = failedState === "CANCELLED";
    const needsOcr = (job.error || "").includes("OCR") || (job.error || "").includes("文字层");
    return (
      <div className="r-radius" style={{ maxWidth: 720, margin: "20px auto", border: "1px solid var(--danger)", padding: "16px 20px" }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8, color: cancelled ? "var(--muted)" : "var(--danger)" }}>{cancelled ? "已停止整理" : "这份资料还没整理好"}</div>
        <div style={{ fontSize: 13, color: cancelled ? "var(--muted)" : "var(--danger)", marginBottom: 10 }}>{cancelled ? "原 PDF 仍然可以阅读，需要时可以重新处理。" : job.error}</div>
        {needsOcr ? (
          <div className="c-muted" style={{ fontSize: 12, marginBottom: 10 }}>原 PDF 还在，你可以先阅读；也请看看页面是否清晰、本地中文 OCR 是否可用。</div>
        ) : null}
        <div>
          <Button
            onClick={async () => {
              try {
                await api.retryJob(job.job_id);
                const projectId = state.activeProject?.project_id;
                api.pollJob(job.job_id, { onProgress: (nextJob) => dispatch({ type: "SET_JOB", job: nextJob, projectId }) });
              } catch (e) {
                toast((e as Error).message || "这次重试没有成功，稍后再试好吗？");
              }
            }}
          >
            {cancelled ? "重新处理" : "再试一次"}
          </Button>
        </div>
      </div>
    );
  }

  return null;
}
