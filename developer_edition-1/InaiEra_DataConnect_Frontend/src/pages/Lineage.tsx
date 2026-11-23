import React, { useState, useEffect } from "react";
import {
  useGetPipelinesQuery, useGetPipelineLineageGraphQuery, useLazyGetAssetLineageQuery,
} from "../store/apiSlice";
import { PH, StatCards, Pill, Loading, ErrBox } from "../components/ui";
import SearchSelect from "../components/SearchSelect";
import { ASSET_BADGE } from "../constants";
import { errorMessage } from "../errors";
import type { Asset, LineageEdge } from "../types/api";

// FE-BUG-20: a node's column — its longest-path distance from a "root" (a
// node with no incoming edge in the currently-loaded graph), via Kahn's
// algorithm. Recomputed fresh from the whole accumulated node/edge set
// after every expand, so an upstream expansion correctly slots new nodes
// to the left and a downstream one to the right regardless of which order
// pieces were fetched in — never assumes the graph is a single straight
// line the way the old renderer (one flat row, an arrow between each pair)
// did, which only worked because the default view happens to be linear.
function computeLevels(nodes: Asset[], edges: LineageEdge[]): Record<string, number> {
  const ids = new Set(nodes.map((n) => n.id));
  const outgoing = new Map<string, string[]>();
  const indegree = new Map<string, number>();
  ids.forEach((id) => { outgoing.set(id, []); indegree.set(id, 0); });
  edges.forEach((e) => {
    if (!ids.has(e.from_asset_id) || !ids.has(e.to_asset_id)) return;
    outgoing.get(e.from_asset_id)?.push(e.to_asset_id);
    indegree.set(e.to_asset_id, (indegree.get(e.to_asset_id) ?? 0) + 1);
  });
  const level = new Map<string, number>();
  const queue: string[] = [...ids].filter((id) => (indegree.get(id) ?? 0) === 0);
  queue.forEach((id) => level.set(id, 0));
  const seen = new Set<string>();
  let i = 0;
  while (i < queue.length) {
    const id = queue[i];
    i += 1;
    if (id === undefined || seen.has(id)) continue;
    seen.add(id);
    for (const next of outgoing.get(id) ?? []) {
      level.set(next, Math.max(level.get(next) ?? 0, (level.get(id) ?? 0) + 1));
      indegree.set(next, (indegree.get(next) ?? 0) - 1);
      if ((indegree.get(next) ?? 0) <= 0 && !seen.has(next)) queue.push(next);
    }
  }
  // A node never reached (only possible if the graph somehow contains a
  // cycle, which lineage edges shouldn't) still needs a level rather than
  // being silently dropped from rendering.
  const result: Record<string, number> = {};
  ids.forEach((id) => { result[id] = level.get(id) ?? 0; });
  return result;
}

interface GraphState { nodes: Asset[]; edges: LineageEdge[] }

function mergeGraph(base: GraphState, extra: { nodes: Asset[]; edges: LineageEdge[] }): GraphState {
  const nodeIds = new Set(base.nodes.map((n) => n.id));
  const edgeIds = new Set(base.edges.map((e) => e.id));
  return {
    nodes: [...base.nodes, ...extra.nodes.filter((n) => !nodeIds.has(n.id))],
    edges: [...base.edges, ...extra.edges.filter((e) => !edgeIds.has(e.id))],
  };
}

/** FE-BUG-20: default view is the pipeline's own bounded chain (source →
 * connector → raw topic → quality → prepared topic — already exactly what
 * the backend materializes per pipeline, and already bounded to that one
 * pipeline's own edges). "Expand upstream"/"expand downstream" on any node
 * fetches ONE more hop from the real API (depth=1, a direction, optionally
 * asset_type) and merges it into the client's accumulated graph — never a
 * request for "the whole graph" with nodes hidden/truncated afterward in
 * React. */
function PipelineGraph({ selId, onAssetsChange }: { selId: string; onAssetsChange: (assets: Asset[]) => void }) {
  const { data: g, isLoading: loading, error } = useGetPipelineLineageGraphQuery(selId, { skip: !selId });
  const [fetchAssetLineage] = useLazyGetAssetLineageQuery();
  const [graph, setGraph] = useState<GraphState | null>(null);
  // `${nodeId}:${direction}` while that one expand is in flight — keeps the
  // UI honest about exactly which button is doing something, without
  // needing per-node component state.
  const [expanding, setExpanding] = useState<string | null>(null);
  // `${nodeId}:${direction}` -> what the server left out of that expansion
  // (a hub asset can have thousands of edges; each hop returns a bounded page,
  // see the lineage route's limit/offset) — drives the "N more" button.
  const [more, setMore] = useState<Record<string, { offset: number; omitted: number }>>({});

  // Never carry one pipeline's accumulated expansions into another's — a
  // fresh default graph resets the accumulated state entirely.
  useEffect(() => {
    setGraph(g ? { nodes: g.nodes || [], edges: g.edges || [] } : null);
    setMore({});
  }, [g, selId]);

  // The asset list under the graph is exactly the assets in this graph (this pipeline's chain, plus anything the user expanded) — see Lineage below.
  useEffect(() => { onAssetsChange(graph?.nodes ?? []); }, [graph, onAssetsChange]);

  const expand = async (assetId: string, direction: "upstream" | "downstream", offset = 0) => {
    const key = `${assetId}:${direction}`;
    setExpanding(key);
    try {
      const extra = await fetchAssetLineage({ id: assetId, depth: 1, direction, offset }).unwrap();
      setGraph((cur) => (cur ? mergeGraph(cur, { nodes: extra.nodes || [], edges: extra.edges || [] }) : cur));
      setMore((cur) => {
        const next = { ...cur };
        if (extra.truncated && extra.next_offset != null) next[key] = { offset: extra.next_offset, omitted: extra.omitted_edges ?? 0 };
        else delete next[key];
        return next;
      });
    } finally {
      setExpanding((k) => (k === key ? null : k));
    }
  };

  if (!selId) return <div className="empty">Select a pipeline (deploy one first to build lineage)</div>;
  if (loading) return <Loading />;
  if (error) return <div className="ab">{errorMessage(error)}</div>;
  if (!graph || !graph.nodes.length) return <div className="empty">No lineage for this pipeline yet — deploy it to materialize the graph</div>;

  const levels = computeLevels(graph.nodes, graph.edges);
  const byLevel = new Map<number, Asset[]>();
  graph.nodes.forEach((n) => {
    const lvl = levels[n.id] ?? 0;
    if (!byLevel.has(lvl)) byLevel.set(lvl, []);
    byLevel.get(lvl)!.push(n);
  });
  const sortedLevels = [...byLevel.keys()].sort((a, b) => a - b);
  const nameById: Record<string, string> = {};
  graph.nodes.forEach((n) => { nameById[n.id] = n.name; });

  return (
    <>
      <div className="lgraph">
        {sortedLevels.map((lvl, i) => (
          <React.Fragment key={lvl}>
            {i > 0 && <div className="larrow">→</div>}
            <div className="lcol">
              {(byLevel.get(lvl) ?? []).map((n) => (
                <div className={`lnode ln-${n.asset_type}`} key={n.id}>
                  <div className="ln-t">{(n.asset_type || "").replace("_", " ")}</div>
                  <div className="ln-n">{(n.name || "").slice(0, 26)}</div>
                  <div className="ln-x">
                    <button
                      className="lexp" title="Expand upstream" disabled={expanding === `${n.id}:upstream`}
                      onClick={() => expand(n.id, "upstream")}
                    >◀ upstream</button>
                    <button
                      className="lexp" title="Expand downstream" disabled={expanding === `${n.id}:downstream`}
                      onClick={() => expand(n.id, "downstream")}
                    >downstream ▶</button>
                    {(["upstream", "downstream"] as const).map((dir) => {
                      const rest = more[`${n.id}:${dir}`];
                      return rest ? (
                        <button
                          key={dir} className="lexp" disabled={expanding === `${n.id}:${dir}`}
                          title={`${rest.omitted} more ${dir} links not shown yet`}
                          aria-label={`Show ${rest.omitted} more ${dir}`}
                          onClick={() => expand(n.id, dir, rest.offset)}
                        >{dir === "upstream" ? `◀ +${rest.omitted} more` : `+${rest.omitted} more ▶`}</button>
                      ) : null;
                    })}
                  </div>
                </div>
              ))}
            </div>
          </React.Fragment>
        ))}
      </div>
      <div className="tbl" role="table" style={{ marginTop: 10 }}>
        <div className="thead" role="row" style={{ gridTemplateColumns: "1fr 1fr 0.8fr" }}><div className="th" role="columnheader">From</div><div className="th" role="columnheader">To</div><div className="th" role="columnheader">Edge</div></div>
        <div className="tbody rows-4" role="rowgroup">
          {(graph.edges || []).map((e, i) => (
            <div className="tr" role="row" key={i} style={{ gridTemplateColumns: "1fr 1fr 0.8fr" }}>
              <div className="tdmn" role="cell">{nameById[e.from_asset_id] || e.from_asset_id}</div>
              <div className="tdmn" role="cell">{nameById[e.to_asset_id] || e.to_asset_id}</div>
              <div className="td" role="cell"><Pill cls="bb">{e.edge_type}</Pill></div>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

export default function Lineage() {
  const { data: pipesData, isLoading: pipesLoading, error } = useGetPipelinesQuery();
  const pipes = pipesData || [];
  const [selId, setSelId] = useState<string | null>(null);
  // The assets of the pipeline being shown, as reported by its graph: the four counts and the table both use these, never every asset in the organisation.
  const [shownAssets, setShownAssets] = useState<Asset[]>([]);

  if (pipesLoading) return <Loading />;
  if (error) return <ErrBox error={error} />;

  const curSel = selId ?? ((pipes[0] && pipes[0].id) || "");

  return (
    <div className="page-fill">
      <PH title="Lineage" sub="Visual data flow from source to sink" />
      <StatCards cards={[
        { k: "bl", l: "Total assets", v: shownAssets.length },
        { k: "gn", l: "Sources", v: shownAssets.filter((a) => a.asset_type === "source").length },
        { k: "am", l: "Topics", v: shownAssets.filter((a) => a.asset_type === "kafka_topic").length },
        { k: "bl", l: "Sinks/Proc", v: shownAssets.filter((a) => ["sink", "processor"].includes(a.asset_type)).length },
      ]} />
      {/* The title and the four counts stay put; only what is below them scrolls (its bar is hidden). */}
      <div className="page-scroll">
      <div className="card">
        <div className="ch">
          <div className="ct">Pipeline flow</div>
          <SearchSelect
            ariaLabel="Select pipeline" placeholder={pipes.length ? "Choose a pipeline…" : "— no pipelines —"} emptyText="No matching pipelines" noun="pipeline" menuMinWidth={280}
            style={{ width: 260 }} disabled={!pipes.length}
            options={pipes.map((p) => ({ value: p.id, label: p.name }))} value={curSel} onChange={setSelId}
          />
        </div>
        <div className="cb"><PipelineGraph selId={curSel} onAssetsChange={setShownAssets} /></div>
      </div>
      <div className="tbl" role="table" style={{ "--cols": "0.7fr 1.8fr 1fr" }}>
        <div className="thead" role="row"><div className="th" role="columnheader">Type</div><div className="th" role="columnheader">Qualified name</div><div className="th" role="columnheader">Name</div></div>
        <div className="tbody rows-4" role="rowgroup">
          {shownAssets.length ? shownAssets.map((a, i) => (
            <div className="tr" role="row" key={i}>
              <div className="td" role="cell"><Pill cls={ASSET_BADGE[a.asset_type as keyof typeof ASSET_BADGE] || "bgr"}>{(a.asset_type || "").replace("_", " ")}</Pill></div>
              <div className="tdmn" role="cell">{a.qualified_name}</div>
              <div className="td" role="cell">{a.name}</div>
            </div>
          )) : <div className="empty">No assets for this pipeline yet — deploy it to create lineage</div>}
        </div>
      </div>
      </div>
    </div>
  );
}
