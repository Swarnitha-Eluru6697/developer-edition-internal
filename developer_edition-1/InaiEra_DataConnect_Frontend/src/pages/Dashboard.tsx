import React, { useCallback, useEffect, useMemo, useRef } from "react";
import {
  useGetPipelinesQuery, useGetRuntimeFlowQuery, useGetRuntimeMetricsQuery, useGetConnectClustersQuery,
} from "../store/apiSlice";
import { CreatedAt, PH, StatCards, Pill, Loading, ErrBox } from "../components/ui";
import { useApp } from "../context/AppContext";
import { useUiActions } from "../context/UiContext";
import { usePolling, useOnVisible } from "../hooks/usePolling";
import { useConnectorSummary } from "../hooks/useConnectorSummary";
import { MONITORING_POLL_MS } from "../pollingPolicy";
import { poolLabel, poolStatusStyle } from "../connectPools";
import { relativeTime } from "../runtimeState";
import StatePill, { RecordErrorsFlag, failingRecords } from "../components/StatePill";
import { connectorStateOf, directionOf, failedTaskCount, flowIndex, isDeployed } from "../connectorRows";
import { NO_VALUE, formatCompact, formatMetricValue } from "../connectorMetrics";
import type { MetricsSummary, RuntimeFlow } from "../types/api";

/*
 * Dashboard — real-time connector and pipeline health.
 *
 * Connector STATE comes from Kafka Connect (via /runtime/flow), the read/written RATES, consumer lag and task counts from
 * Prometheus (via /runtime/connectors/metrics/summary), the validation / ingestion failure counts from the quality and
 * DLQ counters (/runtime/metrics/summary). A value that source couldn't give us is a dash, never a 0 (FE-BUG-16), and a
 * failed poll never keeps showing the last answer as if it were current.
 */

const EMPTY_FLOW: RuntimeFlow = { reachable: false, connect_url: "", bootstrap: "", connectors: [], topics: [], sinks: [], message: "" };

export default function Dashboard({ go }: { go?: (id: "mon") => void }) {
  const { selectConnector } = useUiActions();
  const [pollingInterval, reportPollResult] = usePolling({ enabled: true, baseMs: MONITORING_POLL_MS })   // always live: the Dashboard has no pause switch;

  const { data: pipesData, isLoading: pipesLoading, error: pipesError, refetch: refetchPipes } = useGetPipelinesQuery(undefined, { pollingInterval });
  const { data: flowData, error: flowError, refetch: refetchFlow } = useGetRuntimeFlowQuery(undefined, { pollingInterval });
  const { data: metricsData, error: metricsError, refetch: refetchMetrics } = useGetRuntimeMetricsQuery(undefined, { pollingInterval });
  const { data: clustersData, refetch: refetchClusters } = useGetConnectClustersQuery(undefined, { pollingInterval });
  const summary = useConnectorSummary(pollingInterval);

  useEffect(() => { reportPollResult(pipesError ?? flowError ?? summary.error); }, [pipesError, flowError, summary.error, reportPollResult]);
  useOnVisible(() => { refetchPipes(); refetchFlow(); refetchMetrics(); refetchClusters(); summary.refetch(); });

  const lastGoodFlowAt = useRef<string | null>(null);
  useEffect(() => { if (flowData && !flowError) lastGoodFlowAt.current = new Date().toISOString(); }, [flowData, flowError]);

  const deployed = useMemo(() => (pipesData ?? []).filter(isDeployed), [pipesData]);
  const flow = flowData ?? EMPTY_FLOW;
  const flowStale = Boolean(flowError);
  // Only a real answer can say "unreachable": until the first response (or error) arrives there is nothing to judge, and
  // the placeholder flow above must not be mistaken for one — that flashed the warning on every page refresh.
  const flowUnreachable = Boolean(flowData) && !flowStale && flow.reachable === false;
  const flowUnhealthy = flowStale || flowUnreachable;
  const byName = useMemo(() => flowIndex(flow), [flow]);
  // Connect has answered (or failed to): until then states are "loading", not UNKNOWN, and the cards show a dash.
  const statesSettled = Boolean(flowData) || Boolean(flowError);

  const open = useCallback((connector: string) => { selectConnector(connector); go?.("mon"); }, [selectConnector, go]);

  if (pipesLoading) return <Loading />;
  if (pipesError && !pipesData) return <ErrBox error={pipesError} />;

  const m: Partial<MetricsSummary> = metricsError ? {} : (metricsData ?? {});
  const clusters = clustersData ?? [];

  // ── per-connector rows ──────────────────────────────────────────────────────────────────────────────────────────
  let running = 0, failedConn = 0, failedTasks = 0, read = 0, written = 0, lag = 0, failingConn = 0;
  const rows = deployed.map((p) => {
    const state = connectorStateOf(p, byName, flowUnhealthy);
    const dir = directionOf(p);
    const sample = summary.byName[p.connector_name];
    // a paused connector moves no data: Prometheus keeps answering with its last sample for ~5 minutes, so no rate
    const rate = state === "paused" ? undefined : sample;
    // …but a sink's lag is its real backlog, read live from Kafka offsets (not Prometheus), so it counts whatever the state — a paused sink's
    // waiting records are exactly what this card should show, and the total no longer drops once the states arrive.
    const backlog = dir === "sink" ? sample?.lag ?? null : null;
    if (statesSettled && !flowUnhealthy) {
      if (state === "running" || state === "degraded") running++;
      if (state === "failed") failedConn++;
      failedTasks += failedTaskCount(byName[p.connector_name]);
      if (summary.available && failingRecords(state, sample?.error_rate)) failingConn++;
    }
    if (summary.available) {
      if (dir === "sink") { written += rate?.rate ?? 0; lag += backlog ?? 0; } else read += rate?.rate ?? 0;
    }
    const errorRate = summary.available && failingRecords(state, sample?.error_rate) ? (sample?.error_rate as number) : null;
    return { p, state, dir, rate, backlog, errorRate };
  });
  const unknown = flowUnhealthy || !statesSettled;
  const ratesUnavailable = !summary.available;
  const dash = (v: React.ReactNode, hide: boolean) => (hide ? NO_VALUE : v);

  const prom = summary.data;
  const promNote = !prom || summary.stale ? null
    : !prom.prometheus_enabled ? { cls: "alert-i", title: "Prometheus metrics aren't configured",
      body: "Connector state still comes from Kafka Connect. Read/written rates and lag need DATACONNECT_PROMETHEUS_URL pointing at the Prometheus that scrapes the Connect workers." }
    : !prom.reachable ? { cls: "alert-w", title: "Connector metrics are currently unavailable",
      body: `${prom.error || "Prometheus could not be reached."} Rates and lag show as a dash, never as zero.` }
    : null;

  return (
    <div className="page-fill">
      <PH title="Dashboard" sub="Real-time connector and pipeline health" />
      <div className="page-scroll">
        <StatCards cards={[
          { k: "gn", l: "Running connectors", v: dash(running, unknown), s: `of ${deployed.length} deployed` },
          { k: "rd", l: "Failed connectors", v: dash(failedConn, unknown),
            // "all tasks healthy" is only true when no connector is failing records either (RUNNING tasks can skip every record)
            s: unknown ? "state unknown" : failedTasks ? `${failedTasks} failed task${failedTasks > 1 ? "s" : ""}`
              : failingConn ? `${failingConn} running but failing records` : "all tasks healthy" },
          { k: "gn", l: "Records read", v: dash(<span className="num">{formatMetricValue(read, "records/s")}</span>, ratesUnavailable), s: "source → Kafka · now" },
          { k: "gn", l: "Records written", v: dash(<span className="num">{formatMetricValue(written, "records/s")}</span>, ratesUnavailable), s: "Kafka → sink · now" },
        ]} />
        {/* FE-BUG-17: two independently-sourced failure counts, never one undifferentiated "quarantine" number — a
            DataConnect quality-rule failure (Validation) and a Kafka Connect DLQ record (Ingestion) are different
            concepts with different fixes. */}
        <StatCards cards={[
          { k: "am", l: "Failed tasks", v: dash(failedTasks, unknown) },
          { k: "bl", l: "Consumer lag", v: dash(<span className="num">{formatCompact(lag)}</span>, ratesUnavailable), s: "all sinks · records" },
          { k: "rd", l: "Validation failures", v: m.validation_failure_count ?? NO_VALUE, s: "quality rules · 1h" },
          { k: "rd", l: "Ingestion failures", v: m.ingestion_failure_count ?? NO_VALUE, s: "connector DLQ · 1h" },
        ]} />

        {flowUnhealthy && (
          <div className="alert alert-w" role="status">
            <div className="at">Connector state is currently unknown</div>
            <div className="ab">
              {flowStale
                ? `DataConnect couldn't refresh runtime status just now. Last observed ${relativeTime(lastGoodFlowAt.current)}. Nothing below is shown as healthy until a refresh succeeds.`
                : (flow.message || "Kafka Connect could not be reached.")}
            </div>
          </div>
        )}
        {promNote && (
          <div className={`alert ${promNote.cls}`} role="status"><div className="at">{promNote.title}</div><div className="ab">{promNote.body}</div></div>
        )}

        <div className="tbl tbl-capped" role="table" aria-label="Deployed pipelines"
          style={{ "--cols": "1.8fr 1fr 0.7fr 0.9fr 1fr 1.6fr 0.8fr" } as React.CSSProperties}>
          <div className="thead" role="row">
            <div className="th" role="columnheader">Pipeline</div><div className="th" role="columnheader">Created at</div><div className="th" role="columnheader">Type</div>
            <div className="th" role="columnheader">Worker pool</div><div className="th" role="columnheader">State</div>
            <div className="th" role="columnheader">Records read / written</div><div className="th" role="columnheader" />
          </div>
          <div className="tbody" role="rowgroup">
            {rows.length ? rows.map(({ p, state, dir, rate, backlog, errorRate }) => (
              <div className="tr" role="row" key={p.id}>
                <div className="td" role="cell" style={{ flexDirection: "column", alignItems: "flex-start", justifyContent: "center", gap: 1 }}>
                  <b>{p.name}</b>
                  <span className="tdm mono" style={{ padding: 0, fontSize: 10 }}>{[p.source_system, p.entity].filter(Boolean).join(" · ")}</span>
                </div>
                <div className="tdm" role="cell"><CreatedAt value={p.created_at} /></div>
                <div className="td" role="cell"><Pill cls={dir === "sink" ? "ba" : "bb"}>{dir}</Pill></div>
                <div className="tdm" role="cell">{poolLabel(p.runtime_pool_id)} pool</div>
                <div className="td" role="cell" style={{ gap: 6 }}><StatePill state={state} loading={!statesSettled} />{errorRate !== null && <RecordErrorsFlag errorRate={errorRate} />}</div>
                <div className="td" role="cell">
                  {unknown || ratesUnavailable || rate?.rate === null || rate?.rate === undefined
                    ? <span className="tdm" style={{ padding: 0 }}>{NO_VALUE}
                        {!ratesUnavailable && (backlog ?? 0) > 5000 && <> · <span className="num" style={{ color: "var(--color-error-text)" }}>lag {formatCompact(backlog ?? 0)}</span></>}</span>
                    : dir === "sink"
                      ? <span><b className="num">{formatMetricValue(rate.rate, "records/s")}</b> <span className="tdm" style={{ padding: 0 }}>written</span>
                        {(backlog ?? 0) > 5000 && <> · <span className="num" style={{ color: "var(--color-error-text)" }}>lag {formatCompact(backlog ?? 0)}</span></>}</span>
                      : <span><b className="num">{formatMetricValue(rate.rate, "records/s")}</b> <span className="tdm" style={{ padding: 0 }}>read → Kafka</span></span>}
                </div>
                <div className="td" role="cell" style={{ justifyContent: "flex-end" }}>
                  <button type="button" className="btn-g" onClick={() => open(p.connector_name)}>Metrics →</button>
                </div>
              </div>
            )) : <div className="empty">Deploy a pipeline to see read/written counts</div>}
          </div>
        </div>

        <div className="two">
          <div className="card">
            <div className="ch"><div className="ct">Connect worker pools</div></div>
            <div className="cb">
              {clusters.length ? clusters.map((c) => {
                const style = poolStatusStyle(c.status);
                return (
                  <div className="hrow" key={c.id}>
                    <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                      <span className={`dot ${style.dot}`} />
                      <span style={{ minWidth: 0 }}>
                        {c.name}
                        <span className="tdm" style={{ padding: 0, display: "block", fontSize: 11 }}>
                          {c.workload}{(c.connector_count ?? 0) > 0 ? ` · ${c.connector_count} connector${c.connector_count === 1 ? "" : "s"}` : ""}
                        </span>
                      </span>
                    </span>
                    <Pill cls={style.pill}>{c.status}</Pill>
                  </div>
                );
              }) : <div className="empty">No worker pools reported</div>}
            </div>
          </div>
          <div className="card">
            <div className="ch"><div className="ct">Connector status</div><span className="cs">open a connector on Monitoring</span></div>
            <div className="cb conn-status-list" style={{ maxHeight: 300, overflowY: "auto" }}>
              {rows.length ? rows.map(({ p, state }) => (
                <div className="hrow" key={p.id}>
                  <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                    <button type="button" className="conn-link mono" style={{ fontSize: 11 }}
                      onClick={() => open(p.connector_name)}>{p.connector_name}</button>
                    {flowStale && <span className="tdm" style={{ padding: 0, fontSize: 10 }}> · last observed {relativeTime(lastGoodFlowAt.current)}</span>}
                  </span>
                  <StatePill state={state} loading={!statesSettled} />
                </div>
              )) : <div className="empty">Deploy a pipeline to see connector status</div>}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
