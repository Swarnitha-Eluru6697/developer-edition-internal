/** Lineage tab: the project's ref graph as a left→right layered SVG DAG. */
import { useMemo } from "react";
import type { DbtLineageEdge } from "../../../types";
import { layoutLineage } from "../lineageLayout";

export function DbtLineageGraph({ edges }: { edges: DbtLineageEdge[] }) {
  const { nodes, edges: es, pos, boxW, boxH, width, height } = useMemo(() => layoutLineage(edges), [edges]);
  return (
    <div style={{ overflow: "auto", border: "0.5px solid #E3E8E2", borderRadius: 8, background: "#fafafa" }}>
      <svg width={width} height={height} style={{ display: "block", minWidth: "100%" }}>
        <defs>
          <marker id="dbt-arrow" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">
            <path d="M0,0 L9,4.5 L0,9 z" fill="#94a3b8" />
          </marker>
        </defs>
        {es.map((e, i) => {
          const a = pos[e.s], b = pos[e.t];
          if (!a || !b) return null;
          const x1 = a.x + boxW, y1 = a.y + boxH / 2, x2 = b.x, y2 = b.y + boxH / 2, mx = (x1 + x2) / 2;
          return <path key={i} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} stroke="#cbd5e1" strokeWidth={1.5} fill="none" markerEnd="url(#dbt-arrow)" />;
        })}
        {nodes.map((n) => {
          const p = pos[n];
          return (
            <g key={n}>
              <rect x={p.x} y={p.y} width={boxW} height={boxH} rx={6} fill="#E3F6EA" stroke="#B7E0CB" strokeWidth={1} />
              <text x={p.x + boxW / 2} y={p.y + boxH / 2 + 4} textAnchor="middle" fontSize="11" fontFamily="monospace" fill="#1e40af">
                {n.length > 20 ? n.slice(0, 19) + "…" : n}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
