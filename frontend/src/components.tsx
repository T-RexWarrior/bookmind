import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import DOMPurify from "dompurify";
import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  LoaderCircle,
  Pause,
  Play,
  RotateCcw,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import type { Artifact, Ref } from "./types";
import "katex/dist/katex.min.css";

export function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
export function Spinner({ label = "加载中" }: { label?: string }) {
  return (
    <span className="loading">
      <LoaderCircle className="spin" size={16} />
      {label}
    </span>
  );
}
export function Empty({
  title,
  text,
  children,
}: {
  title: string;
  text?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-symbol">
        <BookOpen size={30} />
      </div>
      <h3>{title}</h3>
      {text && <p>{text}</p>}
      {children}
    </div>
  );
}
export function SourceLinks({
  refs,
  onOpen,
}: {
  refs: Ref[];
  onOpen: (ref: Ref) => void;
}) {
  return (
    <div className="source-links">
      {refs.map((r, i) => (
        <button
          key={r.material_id + ":" + r.page}
          className="source-link"
          onClick={() => onOpen(r)}
          title={r.material_title}
        >
          <BookOpen size={12} />
          来源 {i + 1} · 第 {r.page} 页/节
        </button>
      ))}
    </div>
  );
}

let diagramSerial = 0;
export function Diagram({
  artifact,
  compact = false,
}: {
  artifact: Artifact;
  compact?: boolean;
}) {
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");
  const [step, setStep] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [zoom, setZoom] = useState(1);
  const ref = useRef<HTMLDivElement>(null);
  const p = artifact.payload;
  useEffect(() => {
    let dead = false;
    setSvg("");
    setError("");
    setStep(0);
    setPlaying(false);
    setZoom(1);
    if (artifact.format === "svg") {
      setSvg(
        DOMPurify.sanitize(p.code || "", {
          USE_PROFILES: { svg: true, svgFilters: true },
        }),
      );
      return;
    }
    if (artifact.format === "mermaid") {
      void (async () => {
        try {
          const { default: mermaid } = await import("mermaid");
          mermaid.initialize({
            startOnLoad: false,
            securityLevel: "strict",
            theme: "base",
            fontFamily: "Microsoft YaHei, sans-serif",
            htmlLabels: false,
            flowchart: { htmlLabels: false },
            themeVariables: {
              primaryColor: "#e9f4ef",
              primaryBorderColor: "#36826d",
              primaryTextColor: "#173d33",
              lineColor: "#7d9c90",
              fontSize: "16px",
            },
          });
          const result = await mermaid.render(
            "diagram-" + ++diagramSerial,
            p.code || "",
          );
          if (!dead)
            setSvg(
              DOMPurify.sanitize(result.svg, {
                USE_PROFILES: { svg: true, svgFilters: true },
              }),
            );
        } catch {
          if (!dead) setError("这张图的语法未能正确渲染，请重新生成。");
        }
      })();
    }
    return () => {
      dead = true;
    };
  }, [artifact.id, artifact.format, p.code]);
  const steps = p.steps || [];
  useEffect(() => {
    if (!playing) return;
    const id = setInterval(
      () =>
        setStep((s) => {
          if (s >= steps.length - 1) {
            setPlaying(false);
            return s;
          }
          return s + 1;
        }),
      1800,
    );
    return () => clearInterval(id);
  }, [playing, steps.length]);
  const active = steps[step];
  if (artifact.format !== "steps")
    return (
      <div className={"diagram " + (compact ? "compact" : "")} ref={ref}>
        {error ? (
          <p className="error-inline">{error}</p>
        ) : svg ? (
          <>
            <div className="diagram-zoom">
              <span>可缩放查看细节</span>
              <button
                className="icon-button"
                aria-label="缩小图形"
                disabled={zoom <= 0.75}
                onClick={() => setZoom((v) => Math.max(0.75, v - 0.25))}
              >
                <ZoomOut size={15} />
              </button>
              <button
                className="zoom-value"
                aria-label="重置图形缩放"
                onClick={() => setZoom(1)}
              >
                {Math.round(zoom * 100)}%
              </button>
              <button
                className="icon-button"
                aria-label="放大图形"
                disabled={zoom >= 3}
                onClick={() => setZoom((v) => Math.min(3, v + 0.25))}
              >
                <ZoomIn size={15} />
              </button>
            </div>
            <div className="svg-wrap">
              <div
                className="svg-surface"
                style={{ width: `${zoom * 100}%` }}
                dangerouslySetInnerHTML={{ __html: svg }}
              />
            </div>
          </>
        ) : (
          <Spinner label="正在绘制图形" />
        )}
      </div>
    );
  return (
    <div className={"diagram steps-diagram " + (compact ? "compact" : "")}>
      <svg viewBox="0 0 900 480" role="img" aria-label={artifact.title}>
        <defs>
          <marker
            id={"arrow-" + artifact.id}
            viewBox="0 0 10 10"
            refX="24"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#9caea4" />
          </marker>
        </defs>
        {p.edges?.map((e, i) => {
          const a = p.nodes?.find((n) => n.id === e.from),
            b = p.nodes?.find((n) => n.id === e.to);
          return a && b ? (
            <g key={i}>
              <line
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke="#b4c5bb"
                strokeWidth={2}
                markerEnd={`url(#arrow-${artifact.id})`}
              />
              {e.label && (
                <text
                  x={(a.x + b.x) / 2 + 12}
                  y={(a.y + b.y) / 2}
                  fontSize={15}
                  fill="#7b8e82"
                >
                  {e.label}
                </text>
              )}
            </g>
          ) : null;
        })}
        {p.nodes?.map((n) => (
          <g
            key={n.id}
            className={
              "graph-node " +
              (active?.active.includes(n.id)
                ? "active"
                : active?.visited.includes(n.id)
                  ? "visited"
                  : "")
            }
          >
            <circle cx={n.x} cy={n.y} r={29} />
            <text
              x={n.x}
              y={n.y + 6}
              textAnchor="middle"
              fontSize={n.label.length > 7 ? 13 : 20}
              fontWeight="600"
            >
              {n.label}
            </text>
          </g>
        ))}
      </svg>
      <div className="step-description">
        <span className="eyebrow">
          STEP {String(step + 1).padStart(2, "0")} /{" "}
          {String(steps.length).padStart(2, "0")}
        </span>
        <strong>{active?.title}</strong>
        <p>{active?.description}</p>
        {active?.state && <code>{active.state}</code>}
      </div>
      <div className="step-controls">
        <button
          className="icon-button"
          aria-label="重新演示"
          onClick={() => {
            setStep(0);
            setPlaying(false);
          }}
        >
          <RotateCcw size={16} />
        </button>
        <button
          className="icon-button"
          aria-label="上一步"
          disabled={step === 0}
          onClick={() => {
            setStep((s) => s - 1);
            setPlaying(false);
          }}
        >
          <ArrowLeft size={17} />
        </button>
        <input
          aria-label="演示步骤"
          type="range"
          min={0}
          max={steps.length - 1}
          value={step}
          onChange={(e) => {
            setStep(+e.target.value);
            setPlaying(false);
          }}
        />
        <button
          className="icon-button"
          aria-label="下一步"
          disabled={step >= steps.length - 1}
          onClick={() => {
            setStep((s) => s + 1);
            setPlaying(false);
          }}
        >
          <ArrowRight size={17} />
        </button>
        <button
          className="btn small primary"
          onClick={() => {
            if (step === steps.length - 1) setStep(0);
            setPlaying((v) => !v);
          }}
        >
          {playing ? <Pause size={14} /> : <Play size={14} />}{" "}
          {playing ? "暂停" : "播放"}
        </button>
      </div>
    </div>
  );
}
