import { useEffect, useMemo, useState } from "react";
import { useApp } from "../../store/appStore";
import type { LearningSourceView } from "../../types/blocks";
import { IngestionCard } from "../ingestion/IngestionCard";
import * as api from "../../api/client";
import { toast } from "../../components/ui/primitives";

export function SourceLibrary({
  onSelect,
}: {
  onSelect: (source: LearningSourceView, page?: number) => void;
}) {
  const { state, dispatch } = useApp();
  const [filter, setFilter] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<LearningSourceView["outline"]>([]);
  const active = state.sources.find((source) => source.source_id === state.reader?.sourceId);
  const sources = useMemo(
    () => state.sources.filter((source) => source.title.toLowerCase().includes(filter.toLowerCase())),
    [filter, state.sources],
  );
  useEffect(() => {
    setDraft(active?.outline || []);
    setEditing(false);
  }, [active?.source_id, active?.outline]);

  return (
    <aside className="source-library">
      <div className="source-library__head">
        <div>
          <span className="eyebrow">学习空间</span>
          <h2>资料库</h2>
        </div>
        <button
          className="round-action"
          onClick={() => document.getElementById("learning-source-upload")?.click()}
          aria-label="添加资料"
          title="添加资料"
        >＋</button>
      </div>

      <div className="source-search">
        <span>⌕</span>
        <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="搜索资料" />
      </div>

      <IngestionCard compact />

      <div className="source-list">
        {sources.length ? sources.map((source) => (
          <button
            key={source.source_id}
            className={`source-row ${active?.source_id === source.source_id ? "is-active" : ""}`}
            onClick={() => onSelect(source, state.reader?.sourceId === source.source_id ? state.reader.page : 1)}
          >
            <span className="source-file-icon">{source.source_type || "PDF"}</span>
            <span className="source-row__body">
              <strong>{source.title || source.original_filename || "未命名资料"}</strong>
              <span>{sourceStatus(source)}</span>
            </span>
            <span className={`source-dot ${source.state.toLowerCase()}`} />
          </button>
        )) : (
          <div className="source-empty">
            <div className="source-empty__icon">＋</div>
            <strong>把第一份资料放进来吧</strong>
            <p>文字型、扫描型和复杂排版 PDF 都可以尝试；原文件会先显示，MinerU、文本解析或中文 OCR 会在后台接着整理。</p>
            <button className="btn primary" onClick={() => document.getElementById("learning-source-upload")?.click()}>
              选择 PDF
            </button>
          </div>
        )}
      </div>

      {active && (
        <div className="source-outline">
          <div className="section-title-row">
            <span>资料目录</span>
            <small>{active.section_count ? `${active.section_count} 节` : "解析后生成"}</small>
          </div>
          {active.warnings?.length ? (
            <p className="outline-placeholder" style={{ color: "var(--warning)" }}>{active.warnings[0]}</p>
          ) : null}
          <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
            <button className="btn subtle" onClick={() => setEditing((value) => !value)}>
              {editing ? "取消编辑" : "编辑目录"}
            </button>
            <button className="btn subtle" onClick={async () => {
              try {
                const result = await api.reparseSource(active.source_id);
                const projectId = state.activeProject?.project_id;
                const initial = await api.getJob(result.job_id);
                dispatch({ type: "SET_JOB", job: initial, projectId });
                api.pollJob(result.job_id, { onProgress: (job) => dispatch({ type: "SET_JOB", job, projectId }) });
              } catch (error) {
                toast((error as Error).message || "无法重新处理资料");
              }
            }}>新版重解析</button>
          </div>
          {editing ? (
            <div className="outline-list">
              {draft.map((item, index) => (
                <div key={index} style={{ display: "grid", gridTemplateColumns: "1fr 58px 26px 26px 26px", gap: 5 }}>
                  <input value={item.title} onChange={(event) => setDraft((items) => items.map((old, i) => i === index ? { ...old, title: event.target.value } : old))} />
                  <input type="number" min={1} value={item.page} onChange={(event) => setDraft((items) => items.map((old, i) => i === index ? { ...old, page: Number(event.target.value) } : old))} />
                  <button title="降低层级" disabled={(item.path?.length || 1) <= 1} onClick={() => setDraft((items) => items.map((old, i) => i === index ? { ...old, path: [...(old.path || []).slice(0, -2), old.title] } : old))}>←</button>
                  <button title="增加层级" disabled={index === 0} onClick={() => setDraft((items) => items.map((old, i) => i === index ? { ...old, path: [...(items[index - 1].path || [items[index - 1].title]), old.title] } : old))}>→</button>
                  <button aria-label="删除目录项" onClick={() => setDraft((items) => items.filter((_, i) => i !== index))}>×</button>
                </div>
              ))}
              <div style={{ display: "flex", gap: 8 }}>
                <button className="btn subtle" onClick={() => setDraft((items) => [...items, { title: "新目录项", page: items.at(-1)?.page || 1, path: [] }])}>添加</button>
                <button className="btn primary" onClick={async () => {
                  try {
                    const result = await api.updateSourceOutline(active.source_id, draft);
                    setDraft(result.items);
                    setEditing(false);
                    toast("目录已保存");
                  } catch (error) {
                    toast((error as Error).message || "目录保存失败");
                  }
                }}>保存</button>
              </div>
            </div>
          ) : draft.length ? (
            <div className="outline-list">
              {draft.slice(0, 80).map((item, index) => (
                <button key={`${item.page}-${index}`} onClick={() => onSelect(active, item.page)}>
                  <span>{item.title}</span>
                  <small>{item.page}</small>
                </button>
              ))}
            </div>
          ) : (
            <p className="outline-placeholder">
              {active.state === "SUCCEEDED" ? "没有认出清晰的目录，但仍然可以阅读和提问。" : "我正在读目录和内容，再等一小会儿…"}
            </p>
          )}
        </div>
      )}
    </aside>
  );
}

function sourceStatus(source: LearningSourceView): string {
  if (source.state === "SUCCEEDED") {
    const parts = [];
    if (source.page_count) parts.push(`${source.page_count} 页`);
    if (source.concept_count) parts.push(`${source.concept_count} 个知识点`);
    return parts.join(" · ") || "可阅读、可提问";
  }
  if (source.state === "RUNNING" || source.state === "PENDING") {
    if (source.pages_total) return `${source.checkpoint_stage || source.stage} · ${source.pages_done || 0}/${source.pages_total} 页`;
    return source.stage || "正在慢慢整理";
  }
  return source.stage || "还需要再试一次";
}
