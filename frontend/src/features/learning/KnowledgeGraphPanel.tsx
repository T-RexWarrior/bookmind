import { useEffect, useMemo, useState } from "react";
import * as api from "../../api/client";
import type { KnowledgeGraph } from "../../types/blocks";

export function KnowledgeGraphPanel({ projectId, refreshKey }: { projectId: string; refreshKey: number }) {
  const [graph, setGraph] = useState<KnowledgeGraph | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api.knowledgeGraph(projectId).then((value) => alive && setGraph(value)).catch(() => alive && setGraph(null));
    return () => { alive = false; };
  }, [projectId, refreshKey]);

  const layout = useMemo(() => buildLayout(graph), [graph]);
  const selectedNode = graph?.nodes.find((node) => node.concept_id === selected);

  if (!graph || graph.nodes.length === 0) {
    return <div className="c-muted" style={{ fontSize: 12 }}>资料处理完成后将在这里生成知识图谱</div>;
  }

  return (
    <div>
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        style={{ width: "100%", textAlign: "left", border: 0, padding: 0, background: "transparent", color: "inherit", cursor: "pointer" }}
      >
        <span style={{ fontSize: 13, fontWeight: 500 }}>知识图谱</span>
        <span className="c-muted" style={{ fontSize: 11, marginLeft: 6 }}>
          {graph.stats.concepts} 个概念 · {graph.stats.relations} 条关系 {expanded ? "▴" : "▾"}
        </span>
      </button>
      {expanded && (
        <>
          <div className="bg-panel2 r-radius" style={{ overflow: "auto", maxHeight: 360, marginTop: 8 }}>
            <svg width={layout.width} height={layout.height} role="img" aria-label="资料知识图谱">
              <defs>
                <marker id="kg-arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
                  <path d="M0,0 L6,3 L0,6 Z" fill="var(--muted)" />
                </marker>
              </defs>
              {layout.edges.map((edge, index) => (
                <line key={`${edge.source}-${edge.target}-${index}`} x1={edge.x1} y1={edge.y1} x2={edge.x2} y2={edge.y2}
                  stroke="var(--muted)" strokeOpacity={0.45} markerEnd="url(#kg-arrow)" />
              ))}
              {layout.nodes.map((node) => (
                <g key={node.concept_id} onClick={() => setSelected(node.concept_id)} style={{ cursor: "pointer" }}>
                  <circle cx={node.x} cy={node.y} r={selected === node.concept_id ? 8 : 6}
                    fill={node.source === "GOLD" ? "var(--accent)" : "var(--success, #2f8f63)"} />
                  <text x={node.x + 10} y={node.y + 4} fontSize="11" fill="var(--text)">
                    {node.name.length > 12 ? `${node.name.slice(0, 12)}…` : node.name}
                  </text>
                </g>
              ))}
            </svg>
          </div>
          {selectedNode && (
            <div style={{ fontSize: 12, marginTop: 8, lineHeight: 1.55 }}>
              <strong>{selectedNode.name}</strong>
              <div className="c-muted">{selectedNode.section || selectedNode.chapter}</div>
              {selectedNode.description && <div>{selectedNode.description}</div>}
              {selectedNode.source_refs[0]?.physical_page && (
                <div className="c-muted">来源：第 {selectedNode.source_refs[0].physical_page} 页</div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function buildLayout(graph: KnowledgeGraph | null) {
  if (!graph) return { width: 260, height: 80, nodes: [], edges: [] };
  const visible = graph.nodes.slice(0, 60);
  const chapters = [...new Set(visible.map((node) => node.chapter))];
  const positions = new Map<string, { x: number; y: number }>();
  const nodes = visible.map((node) => {
    const column = chapters.indexOf(node.chapter);
    const row = visible.filter((candidate) => candidate.chapter === node.chapter).indexOf(node);
    const point = { x: 22 + column * 170, y: 28 + row * 38 };
    positions.set(node.concept_id, point);
    return { ...node, ...point };
  });
  const edges = graph.edges.flatMap((edge) => {
    const from = positions.get(edge.source);
    const to = positions.get(edge.target);
    return from && to ? [{ ...edge, x1: from.x, y1: from.y, x2: to.x, y2: to.y }] : [];
  });
  const rows = Math.max(1, ...chapters.map((chapter) => visible.filter((node) => node.chapter === chapter).length));
  return { width: Math.max(260, chapters.length * 170), height: Math.max(80, rows * 38 + 28), nodes, edges };
}
