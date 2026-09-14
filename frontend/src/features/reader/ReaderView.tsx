import { useCallback, useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";
import { sourceFileUrl } from "../../api/client";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

export function ReaderView({
  sourceId,
  title,
  initialPage,
  onPageChange,
  onAskSelection,
}: {
  sourceId: string;
  title: string;
  initialPage: number;
  onPageChange: (page: number) => void;
  onAskSelection: (text: string, page: number) => void;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const [numPages, setNumPages] = useState(0);
  const [page, setPage] = useState(initialPage || 1);
  const [width, setWidth] = useState(760);
  const [zoom, setZoom] = useState(1);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState<{ text: string; left: number; top: number } | null>(null);

  useEffect(() => {
    setPage(initialPage || 1);
    setError("");
  }, [initialPage, sourceId]);

  useEffect(() => {
    const node = viewportRef.current;
    if (!node) return;
    const update = () => setWidth(Math.max(320, node.clientWidth - 48));
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const moveTo = useCallback((next: number) => {
    const safe = Math.max(1, Math.min(numPages || next, next));
    setPage(safe);
    onPageChange(safe);
  }, [numPages, onPageChange]);

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select, [contenteditable='true']")) return;
      if (event.key === "ArrowLeft" || event.key === "PageUp") {
        event.preventDefault();
        moveTo(page - 1);
      } else if (event.key === "ArrowRight" || event.key === "PageDown") {
        event.preventDefault();
        moveTo(page + 1);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [moveTo, page]);

  const captureSelection = () => {
    const selected = window.getSelection();
    if (!selected || selected.isCollapsed || !selected.rangeCount) {
      setSelection(null);
      return;
    }
    const range = selected.getRangeAt(0);
    const ancestor = range.commonAncestorContainer.nodeType === Node.TEXT_NODE
      ? range.commonAncestorContainer.parentElement
      : range.commonAncestorContainer as HTMLElement;
    if (!ancestor || !viewportRef.current?.contains(ancestor)) return;
    const text = selected.toString().replace(/\s+/g, " ").trim().slice(0, 5000);
    if (!text) return;
    const rect = range.getBoundingClientRect();
    setSelection({ text, left: Math.max(12, rect.left + rect.width / 2), top: Math.max(12, rect.top - 10) });
  };

  return (
    <section className="source-viewer" aria-label={`正在阅读 ${title}`}>
      <div className="source-viewer__toolbar">
        <div className="source-viewer__identity">
          <span className="source-type-badge">PDF</span>
          <div>
            <strong>{title}</strong>
            <span>原文件 · 可选择文字后向右侧助手提问</span>
          </div>
        </div>
        <div className="reader-controls">
          <button className="icon-button" onClick={() => setZoom((z) => Math.max(.7, z - .1))} aria-label="缩小">−</button>
          <span>{Math.round(zoom * 100)}%</span>
          <button className="icon-button" onClick={() => setZoom((z) => Math.min(1.6, z + .1))} aria-label="放大">＋</button>
          <span className="reader-divider" />
          <button className="page-nav-button" onClick={() => moveTo(page - 1)} disabled={page <= 1}>← 上一页</button>
          <label className="page-input">
            <input
              value={page}
              inputMode="numeric"
              onChange={(event) => setPage(Math.max(1, Number(event.target.value) || 1))}
              onBlur={() => moveTo(page)}
              onKeyDown={(event) => event.key === "Enter" && moveTo(page)}
              aria-label="当前页码"
            />
            <span>/ {numPages || "—"}</span>
          </label>
          <button className="page-nav-button" onClick={() => moveTo(page + 1)} disabled={!numPages || page >= numPages}>下一页 →</button>
        </div>
      </div>
      <div className="source-viewer__canvas" ref={viewportRef} onMouseUp={captureSelection}>
        {error ? (
          <div className="source-error">
            <strong>暂时无法显示这份资料</strong>
            <p>{error}</p>
          </div>
        ) : (
          <Document
            file={sourceFileUrl(sourceId)}
            onLoadSuccess={({ numPages: count }) => {
              setNumPages(count);
              setError("");
              if (page > count) moveTo(count);
            }}
            onLoadError={(reason) => setError(reason?.message || "无法打开原文件。")}
            loading={<div className="source-loading"><span className="loading-ring" />正在打开原资料…</div>}
          >
            <Page
              pageNumber={page}
              renderTextLayer
              renderAnnotationLayer
              width={Math.min(980, width) * zoom}
              loading={<div className="source-loading">正在渲染第 {page} 页…</div>}
            />
          </Document>
        )}
      </div>
      {selection && (
        <button
          className="selection-ask-button"
          style={{ left: selection.left, top: selection.top }}
          onClick={() => {
            onAskSelection(selection.text, page);
            setSelection(null);
            window.getSelection()?.removeAllRanges();
          }}
        >
          问助手
        </button>
      )}
      {numPages > 0 && !error && (
        <nav className="reader-page-dock" aria-label="PDF 翻页">
          <button onClick={() => moveTo(page - 1)} disabled={page <= 1}>← 上一页</button>
          <strong>第 {page} / {numPages} 页</strong>
          <button onClick={() => moveTo(page + 1)} disabled={page >= numPages}>下一页 →</button>
        </nav>
      )}
    </section>
  );
}
