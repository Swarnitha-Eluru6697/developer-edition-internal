import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useGetPipelinesQuery, useGetRuntimeFlowQuery } from "../store/apiSlice";
import { PH, Loading, ErrBox } from "../components/ui";
import ConnectorMetricsPanel from "../components/ConnectorMetricsPanel";
import MonitoringRail, { type DirFilter, type RailRow } from "../components/MonitoringRail";
import RefreshButton from "../components/RefreshButton";
import { useApp } from "../context/AppContext";
import { useUi, useUiActions } from "../context/UiContext";
import { usePolling, useOnVisible } from "../hooks/usePolling";
import { useConnectorSummary } from "../hooks/useConnectorSummary";
import { MONITORING_POLL_MS } from "../pollingPolicy";
import { poolLabel } from "../connectPools";
import { relativeTime } from "../runtimeState";
import { connectorStateOf, directionOf, flowIndex, isDeployed } from "../connectorRows";
import type { RuntimeFlow } from "../types/api";

// FE-BUG-06: a connector's own state (RUNNING -> task failure -> FAILED) changes on Kafka Connect's side, with no
// browser action involved at all — this is the page whose entire purpose is surfacing that, so it polls
// (MONITORING_POLL_MS, see ../pollingPolicy) instead of waiting for a manual Refresh. The Live / Pause switch in the
// header turns that polling off and, for someone who may control pipelines, also pauses/resumes the selected connector
// (see togglePause below); Refresh always works.
//
// FE-BUG-16: RTK Query does NOT clear a query's `data` just because a later background poll's `error` gets set — the
// previous successful response stays in the cache. Everything shown here therefore checks for a failed poll and renders
// "unknown" / "last observed Xm ago" instead of trusting `data` alone.

const EMPTY_FLOW: RuntimeFlow = { reachable: false, connect_url: "", bootstrap: "", connectors: [], topics: [], sinks: [], message: "" };

export default function Monitoring() {
  const selected = useUi((s) => s.selected);
  const { selectConnector } = useUiActions();
  const [pollingInterval, reportPollResult] = usePolling({ enabled: true, baseMs: MONITORING_POLL_MS });

  const { data: pipesData, isLoading: pipesLoading, error: pipesError, refetch: refetchPipes } = useGetPipelinesQuery(undefined, { pollingInterval });
  const { data: flowData, error: flowError, refetch: refetchFlow } = useGetRuntimeFlowQuery(undefined, { pollingInterval });
  const summary = useConnectorSummary(pollingInterval);

  // The queries poll in lockstep (same interval, same gate), so one outcome is enough to drive the backoff.
  useEffect(() => { reportPollResult(pipesError ?? flowError ?? summary.error); }, [pipesError, flowError, summary.error, reportPollResult]);
  useOnVisible(() => { refetchPipes(); refetchFlow(); summary.refetch(); });

  const lastGoodFlowAt = useRef<string | null>(null);
  useEffect(() => { if (flowData && !flowError) lastGoodFlowAt.current = new Date().toISOString(); }, [flowData, flowError]);

  const [filter, setFilter] = useState("");
  const [dir, setDir] = useState<DirFilter>("all");

  const deployed = useMemo(() => (pipesData ?? []).filter(isDeployed), [pipesData]);
  const flow = flowData ?? EMPTY_FLOW;
  // `flowStale`: this tick's own call failed, so `flow` is whatever was fetched last. `flowUnreachable`: the call worked
  // and the backend's honest answer is that Kafka Connect couldn't be reached. Either way no state from `flow` is trusted.
  const flowStale = Boolean(flowError);
  // Only a real answer can say "unreachable": until the first response (or error) arrives there is nothing to judge, and
  // the placeholder flow above must not be mistaken for one — that flashed the warning on every page refresh.
  const flowUnreachable = Boolean(flowData) && !flowStale && flow.reachable === false;
  const flowUnhealthy = flowStale || flowUnreachable;
  const byName = useMemo(() => flowIndex(flow), [flow]);
  // Connect has answered (or failed to): until then states are "loading", not UNKNOWN, and nothing is auto-selected.
  const statesSettled = Boolean(flowData) || Boolean(flowError);

  const rows: RailRow[] = useMemo(() => deployed.map((p) => {
    const sample = summary.byName[p.connector_name];
    return {
      connector: p.connector_name,
      name: p.name || p.connector_name,
      direction: directionOf(p),
      poolLabel: poolLabel(p.runtime_pool_id),
      state: connectorStateOf(p, byName, flowUnhealthy),
      loading: !statesSettled,
      // a paused connector moves no data: Prometheus keeps answering with its last sample for ~5 minutes, so no rate
      rate: summary.available && connectorStateOf(p, byName, flowUnhealthy) !== "paused" ? (sample?.rate ?? null) : null,
      errorRate: summary.available ? (sample?.error_rate ?? null) : null,
    };
  }), [deployed, byName, flowUnhealthy, statesSettled, summary.byName, summary.available]);

  // Open on a sensible connector (the first running one) rather than an empty panel; keep the choice until that
  // connector no longer exists.
  useEffect(() => {
    // Only a LOADED, genuinely empty list clears the choice — an empty list while pipelines are still loading must not.
    if (!rows.length) { if (pipesData && selected) selectConnector(null); return; }
    if (!statesSettled) return;
    if (!selected || !rows.some((r) => r.connector === selected)) {
      selectConnector((rows.find((r) => r.state === "running") ?? rows[0])!.connector);
    }
  }, [rows, selected, selectConnector, statesSettled, pipesData]);

  if (pipesLoading) return <Loading />;
  if (pipesError && !pipesData) return <ErrBox error={pipesError} />;

  return (
    <div className="page-fill">
      <PH title="Monitoring" sub="Prometheus metrics for each Kafka Connect connector"
        right={<RefreshButton />} />
      <div className="page-scroll mon-page">
        {flowUnhealthy && (
          <div className="alert alert-w" role="status">
            <div className="at">Connector state is currently unknown</div>
            <div className="ab">
              {flowStale
                ? `DataConnect couldn't refresh runtime status just now. Last observed ${relativeTime(lastGoodFlowAt.current)}. Nothing is shown as healthy until a refresh succeeds.`
                : (flow.message || "Kafka Connect could not be reached.")}
            </div>
          </div>
        )}
        <div className="mon">
          <MonitoringRail rows={rows} selected={selected} onSelect={selectConnector}
            filter={filter} onFilter={setFilter} dir={dir} onDir={setDir} />
          <ConnectorMetricsPanel pipelines={deployed} selected={selected} onSelect={selectConnector} />
        </div>
      </div>
    </div>
  );
}
