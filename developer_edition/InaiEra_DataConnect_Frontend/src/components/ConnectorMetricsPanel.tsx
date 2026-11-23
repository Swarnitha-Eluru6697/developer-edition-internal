import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import Select from "./Select";
import { useGetConnectorMetricsQuery, useGetConnectorStatusQuery, usePipelineActionMutation } from "../store/apiSlice";
import { usePolling, useOnVisible } from "../hooks/usePolling";
import { CONNECTOR_METRICS_POLL_MS } from "../pollingPolicy";
import { Drawer, Pill } from "./ui";
import { useApp } from "../context/AppContext";
import { useUi } from "../context/UiContext";
import { relativeTime, runtimePillClass, runtimeStateLabel } from "../runtimeState";
import type { ConnectorMetricRange, ConnectorMetricSeries, ConnectorMetrics, Pipeline } from "../types/api";
import {
  METRIC_RANGES, NO_VALUE, WIDE_METRICS, chartGeometry, formatMetricValue, formatTime, metricTone, nearestPoint,
  abridgeMetricsResponse,
} from "../connectorMetrics";

/*
 * Per-connector Kafka Connect metrics on the Monitoring page, from Prometheus
 * (GET /runtime/connectors/{name}/metrics — app/services/connector_prometheus_metrics.py).
 *
 * Every Connect worker exports its JMX metrics through the Prometheus JMX exporter; Prometheus scrapes them; the
 * backend runs PromQL for the ONE connector selected here (org-scoped — another org's connector 404s). Same honesty
 * rules as the rest of this page (FE-BUG-16): a value Prometheus couldn't give us renders "—", never 0, and a failed
 * poll never keeps showing the previous answer as if it were current.
 *
 * Tiles: click one to enlarge its chart (axis grid, exact value on hover). Throughput and lag tiles are double width.
 */

// ── One chart: area + line, crosshair and hover value ───────────────────────────────────────────────────────────────
function Chart({ metric, big, longRange, hover, onHover }: {
  metric: ConnectorMetricSeries; big: boolean; longRange: boolean; hover: number | null; onHover: (frac: number | null) => void;
}) {
  const points = useMemo(() => metric.series ?? [], [metric.series]);
  const geo = useMemo(() => chartGeometry(points, metric.unit, big), [points, metric.unit, big]);
  // A value the backend reads live (the sink's Consumer lag, from Kafka committed offsets) has no history by design.
  if (metric.history === false) return <div className="mt-empty">Live from Kafka offsets · no history</div>;
  if (!geo) return <div className="mt-empty">No samples in this range</div>;

  const hp = hover !== null ? nearestPoint(points, hover) : null;
  const pct = (t: number) => (geo.x(t) / geo.width) * 100;
  const values = points.map((p) => p.v).filter((v): v is number => v !== null && v !== undefined);
  const label = `${metric.label}: min ${formatMetricValue(Math.min(...values), metric.unit)}, max ${formatMetricValue(Math.max(...values), metric.unit)}, latest ${formatMetricValue(metric.current, metric.unit)}`;
  const onMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const svg = e.currentTarget.querySelector("svg");
    if (!svg) return;
    const r = svg.getBoundingClientRect();
    onHover(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)));
  };

  return (
    <div className="mt-spark" onMouseMove={onMove} onMouseLeave={() => onHover(null)}
      title={geo.hold && geo.last ? `No new samples since ${formatTime(geo.last.t, longRange)} — the dashed line is the last known value, not a measurement.` : undefined}>
      <svg viewBox={`0 0 ${geo.width} ${geo.height}`} preserveAspectRatio="none" role="img" aria-label={label}>
        {geo.grid.map((g) => <line key={g.v} x1="0" x2={geo.width} y1={g.y} y2={g.y} className="mt-gridl" vectorEffect="non-scaling-stroke" />)}
        <line x1="0" x2={geo.width} y1={geo.height - (big ? 10 : 6)} y2={geo.height - (big ? 10 : 6)} className="mt-base" vectorEffect="non-scaling-stroke" />
        {geo.area && <path d={geo.area} className="mt-area" />}
        {geo.hold && <path d={geo.hold} className="mt-hold" vectorEffect="non-scaling-stroke" />}
        <path d={geo.line} className="mt-line" vectorEffect="non-scaling-stroke" />
        {hp && <line x1={geo.x(hp.t)} x2={geo.x(hp.t)} y1="0" y2={geo.height} className="mt-cross" vectorEffect="non-scaling-stroke" />}
      </svg>
      {geo.grid.map((g) => (
        <span key={g.v} className="mt-ylab" style={{ top: `${(g.y / geo.height) * 100}%` }}>{formatMetricValue(g.v, metric.unit)}</span>
      ))}
      {geo.last && !hp && <span className="mt-end" style={{ left: `${pct(geo.last.t)}%`, top: `${(geo.y(geo.last.v) / geo.height) * 100}%` }} />}
      {hp && hp.v !== null && hp.v !== undefined && (
        <span className="mt-dot" style={{ left: `${pct(hp.t)}%`, top: `${(geo.y(hp.v) / geo.height) * 100}%` }} />
      )}
      {hp && big && (
        <div className="mt-tip" role="status" style={{ left: `${Math.min(Math.max(pct(hp.t), 14), 86)}%` }}>
          <strong>{formatMetricValue(hp.v, metric.unit)}</strong><span>{formatTime(hp.t, longRange)}</span>
        </div>
      )}
      <div className="mt-x">
        <span>{formatTime(geo.t0, longRange)}</span>
        {big && <span>{formatTime((geo.t0 + geo.t1) / 2, longRange)}</span>}
        <span>now</span>
      </div>
    </div>
  );
}

function MetricTile({ metric, longRange, dim, stale, idle, big, onToggle }: {
  metric: ConnectorMetricSeries; longRange: boolean; dim: boolean; stale: boolean; idle: boolean; big: boolean; onToggle: () => void;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const points = metric.series ?? [];
  // While hovering a small tile its header shows the hovered sample (and when it was taken) instead of the latest.
  const hp = !dim && !big && hover !== null && points.length > 1 ? nearestPoint(points, hover) : null;
  // A paused connector produces nothing: Prometheus still answers with the last sample it saw (up to ~5 minutes), which
  // would read as a live rate, so the headline is a dash. The chart keeps the history, with its dashed last-known tail.
  // …except a value read live from Kafka (history === false): it is current even while paused — a paused sink's backlog is exactly what to see.
  const live = metric.history === false;
  const shown = idle && !live ? null : hp ? hp.v : metric.current;
  const tone = dim || (idle && !live) ? "" : metricTone(metric.id, metric.current);
  const wide = WIDE_METRICS.has(metric.id);
  const state = dim ? "unavailable" : formatMetricValue(shown, metric.unit);
  return (
    <div
      className={`mt-tile ${big ? "big" : wide ? "wide" : ""} ${tone}`} data-metric={metric.id}
      role="button" tabIndex={0} aria-expanded={big}
      aria-label={`${metric.label}, ${state}. ${big ? "Collapse" : "Expand"} chart`}
      onClick={onToggle}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggle(); } }}
    >
      <div className="mt-h">
        <span className="mt-l" title={metric.description ?? undefined}>
          <span className="mt-lt">{metric.label}</span>{hp && <span className="mt-when">{formatTime(hp.t, longRange)}</span>}
        </span>
        <span className="mt-v" style={dim || stale || (idle && !live) ? { color: "var(--color-text-muted)" } : undefined}>
          {formatMetricValue(dim ? null : shown, metric.unit)}
        </span>
      </div>
      {big && metric.description && <div className="mt-desc">{metric.description}</div>}
      {dim
        ? <div className="mt-empty">Unavailable</div>
        : <Chart metric={metric} big={big} longRange={longRange} hover={hover} onHover={setHover} />}
    </div>
  );
}

/** Kafka Connect numbers a connector's tasks from 0 (that id is what its REST API, its logs and "Restart task" use); the page counts
 *  from 1, so the first task is "Task 1". An id that isn't a plain number is shown as it came. */
function taskNumber(id: unknown): string {
  const text = String(id ?? "").trim();
  const n = Number(text);
  return text !== "" && Number.isInteger(n) && n >= 0 ? String(n + 1) : text;
}

function taskPill(status?: string | null) {
  const s = (status || "unknown").toLowerCase();
  return <Pill cls={runtimePillClass(s)}>{runtimeStateLabel(s)}</Pill>;
}

// ── "Show API response": the real response, abridged (see abridgeMetricsResponse), syntax-coloured as React nodes (no HTML injection) ──

function JsonView({ text }: { text: string }) {
  const nodes: React.ReactNode[] = [];
  const re = /("(?:[^"\\]|\\.)*")(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    if (m[1] !== undefined) {
      nodes.push(<span key={i++} className={m[2] ? "k" : "s"}>{m[1]}</span>);
      if (m[2]) nodes.push(m[2]);
    } else nodes.push(<span key={i++} className="n">{m[3]}</span>);
    last = m.index + m[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return <>{nodes}</>;
}

function Body({ data, stale, paused, lastGoodAt, expanded, setExpanded, pipeline, showJson, setShowJson, canRestart, onRestart, compact }: {
  data: ConnectorMetrics; stale: boolean; paused: boolean; lastGoodAt: string | null;
  expanded: string | null; setExpanded: (id: string | null) => void;
  pipeline: Pipeline | undefined; showJson: boolean; setShowJson: (v: boolean) => void;
  canRestart: boolean; onRestart: (task: string) => void;
  /** In the side panel: only the notices and the metric tiles — no per-task table and no API-response footer. */
  compact: boolean;
}) {
  const metrics = data.metrics ?? [];
  const tasks = data.tasks ?? [];
  // Prometheus labels the one Connect worker a local stack runs every connector on "shared"; that is a scrape label, not a
  // worker pool. The Worker pool column names the pool this connector is assigned to (as the header does) and says it
  // runs on the shared worker; a real per-pool worker label, when there is one, is shown as is.
  const workerOf = (label: string | null | undefined) => (
    !label || label === "shared" ? (data.runtime_pool_id ? `${data.runtime_pool_id} (shared worker)` : null) : label
  );
  const taskColumns = data.task_columns ?? [];
  const liveIds = new Set(metrics.filter((m) => m.history === false).map((m) => m.id));   // read live, so still shown while paused
  const longRange = data.range === "6h" || data.range === "24h";
  const unavailable = !data.prometheus_enabled || !data.reachable;
  const hasAnySample = metrics.some((m) => (m.series ?? []).some((p) => p.v !== null && p.v !== undefined));
  const jsonText = useMemo(() => (showJson ? JSON.stringify(abridgeMetricsResponse(data), null, 2) : ""), [showJson, data]);

  return (
    <>
      {!data.prometheus_enabled && (
        <div className="alert alert-i" role="status">
          <div className="at">Prometheus metrics aren't configured</div>
          <div className="ab">
            Connector status above still comes from Kafka Connect directly. To see throughput, lag and error metrics here,
            point the backend at the Prometheus that scrapes the Connect workers' JMX exporter (<code>DATACONNECT_PROMETHEUS_URL</code>).
          </div>
        </div>
      )}
      {data.prometheus_enabled && !data.reachable && (
        <div className="alert alert-w" role="status">
          <div className="at">Connector metrics are currently unavailable</div>
          <div className="ab">{data.error || "Prometheus could not be reached."} The page retries on the next poll. Values show as a dash, never as zero.</div>
        </div>
      )}
      {stale && !unavailable && (
        <div className="alert alert-w" role="status">
          <div className="at">Showing the last metrics DataConnect received</div>
          <div className="ab">The backend didn't answer the latest poll. These values are from {relativeTime(lastGoodAt)} and are not live.</div>
        </div>
      )}
      {!unavailable && !stale && !hasAnySample && (
        <div className="alert alert-i" role="status">
          <div className="at">No samples yet for this connector</div>
          <div className="ab">
            Prometheus has no series for <code>{data.connector_name}</code> in the last {data.range}. A connector that was just
            deployed shows up after its worker's next scrape (about 15s); if it stays empty, check that the worker pool is
            a Prometheus target.
          </div>
        </div>
      )}

      <div className="mt-grid">
        {metrics.map((m) => (
          <MetricTile
            key={m.id} metric={m} longRange={longRange} dim={unavailable} stale={stale} idle={paused}
            big={expanded === m.id} onToggle={() => setExpanded(expanded === m.id ? null : m.id)}
          />
        ))}
      </div>

      {!compact && !unavailable && tasks.length > 0 && (
        <>
          <div className="cm-sect">Tasks</div>
          <div className="tbl" role="table" aria-label="Per-task metrics"
            style={{ "--cols": `0.6fr 0.9fr 1.3fr ${taskColumns.map(() => "1fr").join(" ")} 1fr`, marginBottom: 0 } as React.CSSProperties}>
            <div className="thead" role="row">
              <div className="th" role="columnheader">Task</div>
              <div className="th" role="columnheader">Status</div>
              <div className="th" role="columnheader">Worker pool</div>
              {taskColumns.map((c) => <div className="th" role="columnheader" key={c.id}>{c.label}</div>)}
              <div className="th" role="columnheader" />
            </div>
            <div className="tbody" role="rowgroup">
              {tasks.map((t) => (
                <div className="tr" role="row" key={t.task}>
                  <div className="td" role="cell" style={{ fontWeight: 600 }}>Task {taskNumber(t.task)}</div>
                  <div className="td" role="cell">{taskPill(t.status)}</div>
                  <div className="tdm" role="cell">{workerOf(t.worker) || NO_VALUE}</div>
                  {taskColumns.map((c) => (
                    <div className="td num" role="cell" key={c.id}>{formatMetricValue(paused && !liveIds.has(c.id) ? null : t.values?.[c.id], c.unit)}</div>
                  ))}
                  <div className="td" role="cell" style={{ justifyContent: "flex-end" }}>
                    {(t.status || "").toLowerCase() === "failed" && canRestart && pipeline && (
                      <button type="button" className="btn-s btn-sm" onClick={() => onRestart(t.task)}
                        title="Restarts this connector's failed tasks">Restart task</button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}

      {!compact && <div className="cm-foot">
        <button type="button" className="btn-g" aria-expanded={showJson} onClick={() => setShowJson(!showJson)}>
          {showJson ? "Hide" : "Show"} API response
        </button>
        <span className="tdm" style={{ padding: 0 }}>Click a tile to enlarge its chart · hover a chart for exact values</span>
      </div>}
      {!compact && showJson && (
        <pre className="json" aria-label="API response">
          {`GET /api/v1/dataconnect/runtime/connectors/${data.connector_name}/metrics?range=${data.range}  →  200\n\n`}
          <JsonView text={jsonText} />
        </pre>
      )}
    </>
  );
}

export default function ConnectorMetricsPanel({
  pipelines, selected, onSelect, variant = "card",
}: {
  /** This org's deployed pipelines (those with a connector_name). */
  pipelines: Pipeline[];
  selected: string | null;
  onSelect: (connectorName: string) => void;
  /** "card": the Monitoring page's card with its own title, connector picker and range tabs. "drawer": inside a side panel that already
   *  names the pipeline — just the range tabs and the metrics. */
  variant?: "card" | "drawer";
}) {
  const { can, toast } = useApp();
  const [range, setRange] = useState<ConnectorMetricRange>("1h");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showJson, setShowJson] = useState(false);
  const [pollingInterval, reportPollResult] = usePolling({ enabled: true, baseMs: CONNECTOR_METRICS_POLL_MS });
  const selectId = useId();
  const connectorName = selected && pipelines.some((p) => p.connector_name === selected) ? selected : null;
  const pipeline = connectorName ? pipelines.find((p) => p.connector_name === connectorName) : undefined;

  const { data, error, isFetching, refetch } = useGetConnectorMetricsQuery(
    { name: connectorName ?? "", range },
    { pollingInterval, skip: !connectorName },
  );
  // The connector's own Kafka Connect status — the real failure trace for the "Task #k failed" notice.
  const { data: status } = useGetConnectorStatusQuery(connectorName ?? "", { pollingInterval, skip: !connectorName });
  const [pipelineAction] = usePipelineActionMutation();
  useEffect(() => { reportPollResult(error); }, [error, reportPollResult]);
  useOnVisible(() => { if (connectorName) refetch(); });
  useEffect(() => { setExpanded(null); }, [connectorName]);

  // FE-BUG-16 pattern: remember when metrics last actually arrived, for "Last observed Xm ago".
  const [lastGoodAt, setLastGoodAt] = useState<string | null>(null);
  useEffect(() => { if (data && !error) setLastGoodAt(new Date().toISOString()); }, [data, error]);

  const options = useMemo(
    () => pipelines.filter((p) => p.connector_name).map((p) => ({ name: p.connector_name as string, label: p.name || p.connector_name })),
    [pipelines],
  );
  // RTK Query keeps the previous arg's data while a new arg loads; never show connector A's numbers under B's name.
  const fresh = data && data.connector_name === connectorName && data.range === range ? data : undefined;
  // While a new range loads, keep showing the previous range's tiles for the SAME connector (dimmed) instead of
  // swapping the whole panel for a one-line "Loading…" — that collapsed the page height and made the page jump.
  const lastShown = useRef<ConnectorMetrics | undefined>(undefined);
  useEffect(() => { if (fresh) lastShown.current = fresh; }, [fresh]);
  const stalePrev = !fresh && lastShown.current?.connector_name === connectorName ? lastShown.current : undefined;
  const current = fresh ?? stalePrev;
  const refreshing = !fresh && Boolean(stalePrev);
  const stale = Boolean(error);
  const staleSince = lastGoodAt;
  // Different connector, nothing to show yet: hold the body at its last height so the layout below stays put.
  const bodyRef = useRef<HTMLDivElement>(null);
  const bodyHeight = useRef(0);
  useEffect(() => { if (current && bodyRef.current) bodyHeight.current = bodyRef.current.offsetHeight; });

  // Kafka Connect says the connector is paused (its own answer, not Prometheus').
  const paused = (status?.observed_state ?? "").toLowerCase() === "paused";
  const failedTasks = (status?.tasks ?? []).filter((t) => (t.state || "").toUpperCase() === "FAILED");
  const traceLine = (failedTasks.find((t) => t.trace)?.trace || "").split("\n")[0]?.slice(0, 260);

  const restart = async (task: string) => {
    if (!pipeline) return;
    try {
      await pipelineAction({ id: pipeline.id, action: "restart" }).unwrap();
      toast(`Restart requested for ${pipeline.name} (task ${taskNumber(task)}). It reports RUNNING after the next scrape.`);
      refetch();
    } catch (e) {
      toast(e instanceof Error ? `Restart failed: ${e.message}` : "Restart failed", "err");
    }
  };

  const rangeTabs = (
    <div className="dc-tabs cm-seg" role="tablist" aria-label="Time range">
      {METRIC_RANGES.map((r) => (
        <button
          key={r.id} type="button" role="tab" aria-selected={range === r.id}
          onClick={() => setRange(r.id)}
        >{r.label}</button>
      ))}
    </div>
  );
  const drawer = variant === "drawer";

  return (
    <section className={drawer ? "cm-drawer" : "card cm-card"} id="connector-metrics" aria-busy={isFetching} aria-labelledby={drawer ? undefined : "cmTitle"} aria-label={drawer ? "Connector metrics" : undefined}>
      {drawer ? (
        <div className="cm-drawer-h">{rangeTabs}</div>
      ) : (
        <div className="ch" style={{ flexWrap: "wrap", gap: 10 }}>
          <div style={{ minWidth: 0 }}>
            <div className="ct" id="cmTitle">{pipeline?.name || "Connector metrics"}</div>
            <div className="cs">Kafka Connect JMX metrics via Prometheus · <code>GET /runtime/connectors/{"{name}"}/metrics</code></div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <label htmlFor={selectId} className="sr-only">Connector</label>
            <Select
              id={selectId} className="cm-select narrow-only" value={connectorName ?? ""} disabled={!options.length}
              onChange={(e) => onSelect(e.target.value)}
            >
              {!connectorName && <option value="">Select a connector…</option>}
              {options.map((o) => <option key={o.name} value={o.name}>{o.label}</option>)}
            </Select>
            {rangeTabs}
          </div>
        </div>
      )}
      <div className="cb" ref={bodyRef} style={{ minHeight: !current && connectorName ? bodyHeight.current || undefined : undefined, opacity: refreshing ? 0.55 : 1, transition: "opacity .15s" }}>
        {!options.length && <div className="empty">Deploy a pipeline to see its connector metrics</div>}
        {options.length > 0 && !connectorName && <div className="empty">Select a connector to see its metrics</div>}
        {connectorName && !current && !error && <div className="empty">Loading metrics…</div>}
        {connectorName && !current && error && (
          <div className="alert alert-w" role="status">
            <div className="at">Couldn't load metrics for this connector</div>
            <div className="ab">DataConnect's backend didn't answer. It will retry automatically.</div>
          </div>
        )}
        {current && (
          <>
            <div className="cm-meta">
              <Pill cls={current.direction === "sink" ? "ba" : "bb"}>{current.direction}</Pill>
              <span className="cm-name mono" title={current.connector_name}>{current.connector_name}</span>
              {current.runtime_pool_id && <span className="tdm" style={{ padding: 0, whiteSpace: "nowrap" }}>· {current.runtime_pool_id}</span>}
              <span className="tdm" style={{ padding: 0, marginLeft: "auto", whiteSpace: "nowrap" }}>
                {stale ? `Last observed ${relativeTime(staleSince)}` : `Updated ${relativeTime(current.observed_at)}`}
                {" · "}step {current.step_seconds}s
              </span>
            </div>
            {failedTasks.length > 0 && (
              <div className="alert alert-w" role="status">
                <div className="at">{failedTasks.length > 1 ? "Tasks" : "Task"} {failedTasks.map((t) => taskNumber(t.id)).join(", ")} failed</div>
                {traceLine && <div className="ab mono" style={{ fontSize: 10.5 }}>{traceLine}</div>}
                {drawer && pipeline && can("dataconnect:pipelines:operate") && (
                  <button type="button" className="btn-s btn-sm" style={{ marginTop: 8 }} onClick={() => restart(String(failedTasks[0]!.id))}
                    title="Restarts this connector's failed tasks">Restart task</button>
                )}
              </div>
            )}
            <Body
              data={current} stale={stale} paused={paused} lastGoodAt={staleSince}
              expanded={expanded} setExpanded={setExpanded} pipeline={pipeline}
              showJson={showJson} setShowJson={setShowJson}
              canRestart={can("dataconnect:pipelines:operate")} onRestart={restart} compact={drawer}
            />
          </>
        )}
      </div>
    </section>
  );
}

/** The same metrics as the Monitoring page, in a side panel opened from a pipeline's row — without leaving the Pipelines page. */
export function ConnectorMetricsDrawer({ pipeline, onClose, onOpenMonitoring }: { pipeline: Pipeline; onClose: () => void; onOpenMonitoring?: () => void }) {
  return (
    <Drawer
      title={<>{pipeline.name} <span className="cs" style={{ fontWeight: 400 }}>· metrics</span></>} onClose={onClose}
      footer={<>
        {onOpenMonitoring && <button className="btn-s" onClick={onOpenMonitoring}>Open in Monitoring</button>}
        <button className="btn-s" onClick={onClose}>Close</button>
      </>}
    >
      <ConnectorMetricsPanel pipelines={[pipeline]} selected={pipeline.connector_name ?? null} onSelect={() => undefined} variant="drawer" />
    </Drawer>
  );
}
