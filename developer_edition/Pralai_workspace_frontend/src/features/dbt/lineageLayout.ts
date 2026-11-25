/** Left→right layered layout for the dbt lineage DAG (pure; rendered as SVG). */
import type { DbtLineageEdge } from "../../types";

export interface LineageLayout {
  nodes: string[];
  edges: { s: string; t: string }[];
  pos: Record<string, { x: number; y: number }>;
  boxW: number;
  boxH: number;
  width: number;
  height: number;
}

const COL_W = 172, ROW_H = 48, BOX_W = 138, BOX_H = 30;

/** The model name without quotes or its schema/catalog prefix. */
export const lineageNodeName = (t: string) => (t.replace(/"/g, "").split(".").pop() || t);

export function layoutLineage(edges: Pick<DbtLineageEdge, "source_table" | "target_table">[]): LineageLayout {
  const es = edges.map((e) => ({ s: lineageNodeName(e.source_table), t: lineageNodeName(e.target_table) }));
  const nodeSet = new Set<string>();
  es.forEach((e) => { nodeSet.add(e.s); nodeSet.add(e.t); });
  const nodes = [...nodeSet];
  // Longest-path layering (column per node).
  const layer: Record<string, number> = {};
  nodes.forEach((n) => (layer[n] = 0));
  for (let i = 0; i < nodes.length; i++) {
    let changed = false;
    for (const e of es) {
      if ((layer[e.t] ?? 0) < (layer[e.s] ?? 0) + 1) { layer[e.t] = layer[e.s] + 1; changed = true; }
    }
    if (!changed) break;
  }
  const byLayer: Record<number, string[]> = {};
  nodes.forEach((n) => { (byLayer[layer[n]] ||= []).push(n); });
  const pos: Record<string, { x: number; y: number }> = {};
  Object.entries(byLayer).forEach(([l, ns]) => ns.forEach((n, i) => { pos[n] = { x: Number(l) * COL_W + 12, y: i * ROW_H + 12 }; }));
  const maxLayer = Math.max(0, ...nodes.map((n) => layer[n]));
  const maxRows = Math.max(1, ...Object.values(byLayer).map((a) => a.length));
  return { nodes, edges: es, pos, boxW: BOX_W, boxH: BOX_H, width: (maxLayer + 1) * COL_W + 24, height: maxRows * ROW_H + 24 };
}
