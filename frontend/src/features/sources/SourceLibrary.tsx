import { useMemo, useState } from "react";
import { useApp } from "../../store/appStore";
import type { LearningSourceView } from "../../types/blocks";
import { IngestionCard } from "../ingestion/IngestionCard";

export function SourceLibrary({
  onSelect,
}: {
  onSelect: (source: LearningSourceView, page?: number) => void;
}) {
  const { state } = useApp();
  const [filter, setFilter] = useState("");
  const active = state.sources.find((source) => source.source_id === state.reader?.sourceId);
  const sources = useMemo(
    () => state.sources.filter((source) => source.title.toLowerCase().includes(filter.toLowerCase())),
    [filter, state.sources],
  );

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
          {active.outline?.length ? (
            <div className="outline-list">
              {active.outline.slice(0, 80).map((item, index) => (
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
  if (source.state === "RUNNING" || source.state === "PENDING") return source.stage || "正在慢慢整理";
  return source.stage || "还需要再试一次";
}
