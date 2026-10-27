import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  GlobalWorkerOptions,
  getDocument,
  TextLayer,
} from "pdfjs-dist/legacy/build/pdf.mjs";
import type { PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/legacy/build/pdf.worker.min.mjs?url";
import "pdfjs-dist/web/pdf_viewer.css";
import { api } from "./api";
import { Spinner } from "./components";

GlobalWorkerOptions.workerSrc = workerUrl;

type Layout = {
  width: number;
  height: number;
  rotation: number;
  spans: {
    text: string;
    x: number;
    y: number;
    width: number;
    size: number;
    bold: boolean;
  }[];
};
const preferenceKey = (url: string) => "studytutor.pdf-display:" + url;
function rememberedCompatibility(url: string, preferred: boolean) {
  try {
    const value = sessionStorage.getItem(preferenceKey(url));
    return value === "compatible" || (value !== "standard" && preferred);
  } catch {
    return preferred;
  }
}

function ServerTextLayer({ endpoint }: { endpoint: string }) {
  const [layout, setLayout] = useState<Layout>();
  useEffect(() => {
    const controller = new AbortController();
    setLayout(undefined);
    api<Layout>(endpoint + "/layout", { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setLayout(value);
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          console.warn("PDF text selection unavailable", error);
      });
    return () => controller.abort();
  }, [endpoint]);
  if (!layout?.spans.length) return null;
  return (
    <svg
      className="pdf-server-text"
      viewBox={`0 0 ${layout.width} ${layout.height}`}
      preserveAspectRatio="none"
      aria-label="本页可选择文字"
    >
      {layout.spans.map((s, i) => (
        <text
          key={i}
          x={s.x}
          y={s.y}
          textLength={s.width}
          lengthAdjust="spacingAndGlyphs"
          fontSize={s.size}
          fontWeight={s.bold ? 700 : 400}
          transform={
            layout.rotation
              ? `rotate(${layout.rotation} ${s.x} ${s.y})`
              : undefined
          }
        >
          {s.text}
        </text>
      ))}
    </svg>
  );
}

export default function PdfReader(props: {
  url: string;
  page: number;
  preferCompatibility?: boolean;
}) {
  // Material changes must not inherit an old document/worker or render state.
  return <Reader key={props.url} {...props} />;
}

function Reader({
  url,
  page,
  preferCompatibility = false,
}: {
  url: string;
  page: number;
  preferCompatibility?: boolean;
}) {
  const [doc, setDoc] = useState<PDFDocumentProxy>();
  const [compatible, setCompatible] = useState(() =>
    rememberedCompatibility(url, preferCompatibility),
  );
  const [textFallback, setTextFallback] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  const [width, setWidth] = useState(700);
  const [retry, setRetry] = useState(0);
  const box = useRef<HTMLDivElement>(null),
    canvas = useRef<HTMLCanvasElement>(null),
    text = useRef<HTMLDivElement>(null);
  const renderRef = useRef<RenderTask | undefined>(undefined);
  const mounted = useRef(true);
  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const parts = url.split("/");
  const endpoint = `/spaces/${parts[2]}/materials/${parts[3]}/pages/${page}`;
  const imageWidth = Math.max(
    640,
    Math.min(
      2400,
      Math.ceil((width * Math.min(window.devicePixelRatio || 1, 2)) / 160) *
        160,
    ),
  );
  const imageUrl = `/api${endpoint}/image?width=${imageWidth}&retry=${retry}`;

  const setDisplayMode = (value: boolean) => {
    setError("");
    setBusy(true);
    setTextFallback(false);
    setCompatible(value);
    setRetry((v) => v + 1);
    try {
      sessionStorage.setItem(
        preferenceKey(url),
        value ? "compatible" : "standard",
      );
    } catch {
      /* Embedded browsers may disable session storage. */
    }
  };
  const useCompatibility = (stage: string, cause: unknown) => {
    if (!mounted.current) return;
    console.warn(`PDF ${stage}; switching to compatible display`, cause);
    setDisplayMode(true);
  };

  useEffect(() => {
    if (!box.current) return;
    const observer = new ResizeObserver((entries) =>
      setWidth(Math.max(260, Math.floor(entries[0].contentRect.width))),
    );
    observer.observe(box.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    setDoc(undefined);
    if (compatible) return;
    let dead = false;
    setBusy(true);
    setError("");
    const timeout = setTimeout(() => {
      if (!dead)
        useCompatibility("open timed out", "PDF loading exceeded 15 seconds");
    }, 15000);
    let loading: ReturnType<typeof getDocument>;
    try {
      loading = getDocument({
        url,
        cMapUrl: "/pdf/cmaps/",
        cMapPacked: true,
        standardFontDataUrl: "/pdf/standard_fonts/",
        wasmUrl: "/pdf/wasm/",
      });
      loading.promise
        .then((value) => {
          clearTimeout(timeout);
          if (!dead) setDoc(value);
        })
        .catch((cause) => {
          clearTimeout(timeout);
          if (!dead) useCompatibility("open failed", cause);
        });
    } catch (cause) {
      clearTimeout(timeout);
      useCompatibility("initialization failed", cause);
    }
    return () => {
      dead = true;
      clearTimeout(timeout);
      void loading?.destroy().catch(() => undefined);
    };
  }, [url, compatible]);

  useEffect(() => {
    if (compatible || !doc || !canvas.current || !text.current) return;
    let dead = false;
    let render: RenderTask | undefined, layer: TextLayer | undefined;
    let selectionTimeout: ReturnType<typeof setTimeout> | undefined;
    let selectionExpired = false;
    setBusy(true);
    setError("");
    setTextFallback(false);
    const timeout = setTimeout(() => {
      if (!dead)
        useCompatibility("paint timed out", "PDF paint exceeded 15 seconds");
    }, 15000);
    void (async () => {
      try {
        // Await the canceled paint before reusing its canvas. Delayed font or
        // image decoding must not keep drawing after a quick page change.
        const previous = renderRef.current;
        previous?.cancel();
        await previous?.promise.catch(() => undefined);
        if (dead) return;
        const p = await doc.getPage(page);
        if (dead) return;
        const viewport = p.getViewport({
          scale: width / p.getViewport({ scale: 1 }).width,
        });
        const c = canvas.current,
          el = text.current;
        // Refs detach before passive effect cleanup. A worker reply can arrive
        // in that interval when changing materials, views, or routes.
        if (!c || !el || !mounted.current) return;
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        c.width = Math.floor(viewport.width * ratio);
        c.height = Math.floor(viewport.height * ratio);
        c.style.width = `${viewport.width}px`;
        c.style.height = `${viewport.height}px`;
        el.innerHTML = "";
        el.style.setProperty("--scale-factor", String(viewport.scale));
        el.style.setProperty("--total-scale-factor", String(viewport.scale));
        el.style.width = `${viewport.width}px`;
        el.style.height = `${viewport.height}px`;
        const context = c.getContext("2d");
        if (!context) throw new Error("Browser canvas is unavailable");
        render = p.render({
          canvas: c,
          canvasContext: context,
          viewport,
          transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
        });
        renderRef.current = render;
        await render.promise;
        clearTimeout(timeout);
        if (dead) return;
        setBusy(false);
        // Failure of text selection must not hide an already painted page.
        try {
          await Promise.race([
            (async () => {
              const content = await p.getTextContent();
              if (dead || selectionExpired) return;
              layer = new TextLayer({
                textContentSource: content,
                container: el,
                viewport,
              });
              await layer.render();
            })(),
            new Promise<never>((_, reject) => {
              selectionTimeout = setTimeout(() => {
                selectionExpired = true;
                layer?.cancel();
                reject(new Error("PDF text selection timed out"));
              }, 8000);
            }),
          ]);
        } catch (cause) {
          selectionExpired = true;
          if (!dead) {
            layer?.cancel();
            el.innerHTML = "";
            console.warn(
              "PDF text layer; using original text positions",
              cause,
            );
            setTextFallback(true);
          }
        } finally {
          clearTimeout(selectionTimeout);
        }
      } catch (cause) {
        clearTimeout(timeout);
        if (
          !dead &&
          !(
            cause instanceof Error &&
            cause.name === "RenderingCancelledException"
          )
        )
          useCompatibility("paint failed", cause);
      }
    })();
    return () => {
      dead = true;
      selectionExpired = true;
      clearTimeout(timeout);
      clearTimeout(selectionTimeout);
      render?.cancel();
      layer?.cancel();
    };
  }, [doc, page, width, compatible]);

  useEffect(() => {
    if (compatible) {
      setBusy(true);
      setError("");
    }
  }, [compatible, imageUrl]);

  return (
    <div className="pdf-reader" ref={box}>
      <div className="pdf-display-bar">
        <span>
          {compatible
            ? "兼容显示"
            : textFallback
              ? "标准显示 · 兼容文字选区"
              : "标准显示"}
        </span>
        <button onClick={() => setDisplayMode(!compatible)}>
          {compatible ? "切换标准显示" : "切换兼容显示"}
        </button>
      </div>
      {error && (
        <div className="error-inline">
          {error}
          <button
            className="text-button"
            onClick={() => {
              setError("");
              setBusy(true);
              setRetry((v) => v + 1);
            }}
          >
            重新加载这一页
          </button>
        </div>
      )}
      {busy && !error && (
        <div className="pdf-loading">
          <Spinner label="正在载入原文" />
        </div>
      )}
      {compatible ? (
        <div className="pdf-paper pdf-compatible" key={imageUrl}>
          <img
            src={imageUrl}
            alt={`教材原版第 ${page} 页`}
            draggable={false}
            onLoad={() => {
              setBusy(false);
              setError("");
            }}
            onError={() => {
              setBusy(false);
              setError(
                "这页暂时无法显示。请重试，或使用上方的文字视图继续阅读。",
              );
            }}
          />
          <ServerTextLayer endpoint={endpoint} />
        </div>
      ) : (
        <div
          className="pdf-paper pdf-standard"
          style={{ visibility: busy ? "hidden" : "visible" }}
        >
          <canvas ref={canvas} />
          <div ref={text} className="textLayer" />
          {textFallback && <ServerTextLayer endpoint={endpoint} />}
        </div>
      )}
    </div>
  );
}
