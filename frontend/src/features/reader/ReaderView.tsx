import { useCallback, useEffect, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import type { PDFDocumentProxy } from "pdfjs-dist";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";
import { getSourceQuality, getSourceTextLayer, searchSource, sourceFileUrl } from "../../api/client";

const pdfWorkerUrl = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
);
// The worker was once served by Windows as text/plain. Keep a deliberate URL
// version so an already-open browser does not reuse that stale module cache
// after the server-side MIME correction.
pdfWorkerUrl.searchParams.set("v", "mime-fix-1");
pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl.toString();

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
  const [pdfDocument, setPdfDocument] = useState<PDFDocumentProxy | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchPages, setSearchPages] = useState<number[]>([]);
  const [searchIndex, setSearchIndex] = useState(0);
  const [printedPages, setPrintedPages] = useState<Record<number, string>>({});
  const [ocrLayer, setOcrLayer] = useState<Awaited<ReturnType<typeof getSourceTextLayer>> | null>(null);

  useEffect(() => {
    setPage(initialPage || 1);
    setError("");
  }, [initialPage, sourceId]);

  useEffect(() => {
    void getSourceQuality(sourceId).then((quality) => {
      setPrintedPages(Object.fromEntries(quality.pages.filter((item) => item.printed_page).map((item) => [item.page, item.printed_page!])))
    }).catch(() => setPrintedPages({}));
  }, [sourceId]);

  useEffect(() => {
    setOcrLayer(null);
    void getSourceTextLayer(sourceId, page).then(setOcrLayer).catch(() => setOcrLayer(null));
  }, [sourceId, page]);

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

  const runSearch = async () => {
    const query = searchQuery.trim().toLocaleLowerCase();
    if (!query || !pdfDocument) return;
    setSearching(true);
    const matches: number[] = [];
    try {
      for (let pageNumber = 1; pageNumber <= pdfDocument.numPages; pageNumber += 1) {
        const pdfPage = await pdfDocument.getPage(pageNumber);
        const content = await pdfPage.getTextContent();
        const text = content.items.map((item) => "str" in item ? item.str : "").join(" ").toLocaleLowerCase();
        if (text.includes(query)) matches.push(pageNumber);
      }
      const parsed = await searchSource(sourceId, searchQuery.trim()).catch(() => ({ results: [] }));
      const combined = [...new Set([...matches, ...parsed.results.map((result) => result.page)])].sort((a, b) => a - b);
      setSearchPages(combined);
      setSearchIndex(0);
      if (combined[0]) moveTo(combined[0]);
    } finally {
      setSearching(false);
    }
  };

  const moveSearch = (offset: number) => {
    if (!searchPages.length) return;
    const next = (searchIndex + offset + searchPages.length) % searchPages.length;
    setSearchIndex(next);
    moveTo(searchPages[next]);
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
          <label className="page-input" title="在 PDF 文字层中搜索">
            <input
              style={{ width: 110, textAlign: "left" }}
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && void runSearch()}
              placeholder="搜索正文"
            />
          </label>
          <button className="icon-button" onClick={() => void runSearch()} disabled={searching || !searchQuery.trim()} aria-label="搜索">⌕</button>
          {searchPages.length ? <>
            <button className="icon-button" onClick={() => moveSearch(-1)} aria-label="上一个搜索结果">↑</button>
            <span>{searchIndex + 1}/{searchPages.length}</span>
            <button className="icon-button" onClick={() => moveSearch(1)} aria-label="下一个搜索结果">↓</button>
          </> : null}
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
            onLoadSuccess={(loadedDocument) => {
              const count = loadedDocument.numPages;
              setPdfDocument(loadedDocument);
              setNumPages(count);
              setError("");
              if (page > count) moveTo(count);
            }}
            onLoadError={(reason) => setError(reason?.message || "无法打开原文件。")}
            loading={<div className="source-loading"><span className="loading-ring" />正在打开原资料…</div>}
          >
            <div style={{ position: "relative" }}>
              <Page
                pageNumber={page}
                renderTextLayer
                renderAnnotationLayer
                width={Math.min(980, width) * zoom}
                loading={<div className="source-loading">正在渲染第 {page} 页…</div>}
              />
              {ocrLayer?.ready && ocrLayer.parser !== "pypdf" && ocrLayer.width && ocrLayer.height && ocrLayer.blocks.length ? (
                <div aria-label="OCR 可选择文字层" style={{ position: "absolute", inset: 0, zIndex: 3, pointerEvents: "none" }}>
                  {ocrLayer.blocks.map((block) => {
                    const [x0, y0, x1, y1] = block.bbox;
                    return <span key={block.block_id} style={{
                      position: "absolute", left: `${x0 / ocrLayer.width! * 100}%`, top: `${y0 / ocrLayer.height! * 100}%`,
                      width: `${(x1 - x0) / ocrLayer.width! * 100}%`, height: `${(y1 - y0) / ocrLayer.height! * 100}%`,
                      color: "transparent", userSelect: "text", pointerEvents: "auto", overflow: "hidden",
                      fontSize: `${Math.max(8, (y1 - y0) / ocrLayer.height! * Math.min(980, width) * zoom)}px`, lineHeight: 1,
                    }}>{block.text}</span>;
                  })}
                </div>
              ) : null}
            </div>
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
          <strong>第 {page} / {numPages} 页{printedPages[page] ? ` · 印刷页 ${printedPages[page]}` : ""}</strong>
          <button onClick={() => moveTo(page + 1)} disabled={page >= numPages}>下一页 →</button>
        </nav>
      )}
    </section>
  );
}
