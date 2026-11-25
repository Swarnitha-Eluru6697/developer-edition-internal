/*
 * Pure helpers for the Monitoring page's per-connector Prometheus metrics
 * (components/ConnectorMetricsPanel.tsx) — kept separate so they're unit-testable
 * without a DOM (tests/unit/connectorMetrics.spec.ts).
 */
import type { ConnectorMetricPoint, ConnectorMetricRange, ConnectorMetrics } from "./types/api";

export const METRIC_RANGES: { id: ConnectorMetricRange; label: string }[] = [
  { id: "15m", label: "15m" },
  { id: "1h", label: "1h" },
  { id: "6h", label: "6h" },
  { id: "24h", label: "24h" },
];

export const NO_VALUE = "—";

export function formatMetricValue(v: number | null | undefined, unit: string): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return NO_VALUE;
  const compact = (n: number, digits = 1) => {
    const abs = Math.abs(n);
    if (abs >= 1e9) return `${(n / 1e9).toFixed(digits)}B`;
    if (abs >= 1e6) return `${(n / 1e6).toFixed(digits)}M`;
    if (abs >= 1e4) return `${(n / 1e3).toFixed(digits)}k`;
    if (abs >= 100 || Number.isInteger(n)) return Math.round(n).toLocaleString();
    return n.toFixed(abs >= 10 ? 1 : 2);
  };
  switch (unit) {
    case "records/s": return `${compact(v)}/s`;
    case "errors/s": return `${compact(v)}/s`;
    case "ms": return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v >= 10 ? Math.round(v) : v.toFixed(1)} ms`;
    case "ratio": return `${(v * (v <= 1 ? 100 : 1)).toFixed(v >= 0.995 || v === 0 ? 0 : 1)}%`;
    case "tasks": return String(Math.round(v));
    default: return compact(v, 1);
  }
}

export function formatTime(t: number, withDate: boolean): string {
  const d = new Date(t * 1000);
  return withDate
    ? d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// ── Tiles ─────────────────────────────────────────────────────────────────────────────────────────────────────────

/** Throughput and lag get a double-width tile on the Monitoring page; the rest are single. */
export const WIDE_METRICS: ReadonlySet<string> = new Set([
  "source_poll_rate", "source_write_rate", "sink_read_rate", "sink_send_rate", "sink_consumer_lag",
]);

export type TileTone = "" | "bad" | "warn";

/** Colour a tile's value only when the number itself says something is wrong. No sample (null) is never coloured. */
export function metricTone(id: string, current: number | null | undefined): TileTone {
  if (current === null || current === undefined || !Number.isFinite(current)) return "";
  if (id === "running_ratio" && current < 0.99) return "bad";
  if (id === "record_error_rate" && current > 1) return "warn";
  if (id === "sink_consumer_lag" && current > 5000) return "warn";
  return "";
}

// ── Chart geometry (pure, so the line/area/grid maths is testable without a DOM) ───────────────────────────────────

export interface MetricPoint { t: number; v?: number | null }

export interface ChartGeometry {
  width: number;
  height: number;
  t0: number;
  t1: number;
  /** SVG path of the line; a missing sample breaks the line instead of being drawn as zero. */
  line: string;
  area: string;
  grid: { v: number; y: number }[];
  x: (t: number) => number;
  y: (v: number) => number;
  /** The newest real sample, for the "now" dot. */
  last: { t: number; v: number } | null;
  /**
   * When the newest samples are missing (a paused connector reports nothing), a flat path from the last real sample to
   * the right edge at that sample's value. It is drawn dashed and faded — "last known value", never a measurement —
   * so the chart is not simply cut off. Empty when the series reaches the right edge. Gaps in the MIDDLE stay gaps.
   */
  hold: string;
}

export function chartGeometry(points: readonly MetricPoint[], unit: string, big: boolean): ChartGeometry | null {
  const real = points.filter((p): p is { t: number; v: number } => p.v !== null && p.v !== undefined && Number.isFinite(p.v));
  if (points.length < 2 || real.length === 0) return null;
  const width = 600;
  const height = big ? 200 : 64;
  const padY = big ? 10 : 6;
  const t0 = points[0]!.t;
  const t1 = points[points.length - 1]!.t;
  const values = real.map((p) => p.v);
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (unit !== "ms") lo = Math.min(0, lo);               // throughput, lag and counts read against zero
  if (unit === "ratio") { lo = 0; hi = Math.max(1, hi); }
  if (hi === lo) hi = lo + 1;
  hi += (hi - lo) * 0.08;                                  // headroom so the peak isn't glued to the top edge
  const x = (t: number) => ((t - t0) / Math.max(1, t1 - t0)) * width;
  const y = (v: number) => height - padY - ((v - lo) / (hi - lo)) * (height - padY * 2);

  let line = "";
  let pen = false;
  for (const p of points) {
    if (p.v === null || p.v === undefined || !Number.isFinite(p.v)) { pen = false; continue; }
    line += `${pen ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`;
    pen = true;
  }
  const baseY = y(Math.max(lo, 0));
  const area = real.length > 1
    ? `M${x(real[0]!.t).toFixed(1)},${baseY.toFixed(1)}` + real.map((p) => `L${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("")
      + `L${x(real[real.length - 1]!.t).toFixed(1)},${baseY.toFixed(1)}Z`
    : "";
  const grid = big ? [0.25, 0.5, 0.75, 1].map((f) => { const v = lo + (hi - lo) * f; return { v, y: y(v) }; }) : [];
  const last = real[real.length - 1] ?? null;
  const hold = last && last.t < t1 ? `M${x(last.t).toFixed(1)},${y(last.v).toFixed(1)}L${width},${y(last.v).toFixed(1)}` : "";
  return { width, height, t0, t1, line, area, grid, x, y, last, hold };
}

/** The sample closest in time to `frac` (0..1 across the chart); null when there are no samples at all. */
export function nearestPoint(points: readonly MetricPoint[], frac: number): MetricPoint | null {
  if (!points.length) return null;
  const t0 = points[0]!.t;
  const t1 = points[points.length - 1]!.t;
  const target = t0 + Math.min(1, Math.max(0, frac)) * (t1 - t0);
  return points.reduce((best, p) => (Math.abs(p.t - target) < Math.abs(best.t - target) ? p : best), points[0]!);
}

/** Same `compact` the formatter uses, exported for the Dashboard's totals. */
export function formatCompact(n: number): string {
  return formatMetricValue(n, "count");
}

/**
 * "Show API response": the real response, abridged for display — each series longer than three points is cut to its first two and
 * last points with a marker between, and every number is rounded to three decimals. Typed end to end on the generated
 * ConnectorMetrics and NON-mutating (the old version deep-copied through JSON and edited the copy as `any`). The result is
 * `unknown` on purpose: a series may now hold the marker string, and the value is only ever stringified for display.
 */
export function abridgeMetricsResponse(data: ConnectorMetrics): unknown {
  const round = <T,>(v: T): T | number => (typeof v === "number" ? +v.toFixed(3) : v);
  const roundPoint = (p: ConnectorMetricPoint): ConnectorMetricPoint => ({ ...p, v: round(p.v) });

  const metrics = data.metrics?.map((m) => {
    const points = m.series;
    let series: (ConnectorMetricPoint | string)[] | undefined;
    if (points !== undefined) {
      const shown: (ConnectorMetricPoint | string)[] = points.length > 3
        ? [...points.slice(0, 2), `… ${points.length - 3} more points …`, ...points.slice(-1)]
        : points;
      series = shown.map((p) => (typeof p === "string" ? p : roundPoint(p)));
    }
    return { ...m, current: round(m.current), ...(series !== undefined ? { series } : {}) };
  });
  const tasks = data.tasks?.map((t) =>
    t.values ? { ...t, values: Object.fromEntries(Object.entries(t.values).map(([k, v]) => [k, round(v)])) } : t);
  return { ...data, ...(metrics ? { metrics } : {}), ...(tasks ? { tasks } : {}) };
}
