/*
 * Value formatting for the Monitoring page's per-connector Prometheus metrics (src/connectorMetrics.ts).
 * A missing value is a dash — never 0, which would read as "the connector is idle".
 */
import { test, expect } from "@playwright/test";
import { formatMetricValue, METRIC_RANGES } from "../../src/connectorMetrics";

test("a value Prometheus didn't give us renders as a dash, never 0", () => {
  expect(formatMetricValue(null, "records/s")).toBe("—");
  expect(formatMetricValue(undefined, "count")).toBe("—");
  expect(formatMetricValue(Number.NaN, "ms")).toBe("—");
  expect(formatMetricValue(0, "records/s")).toBe("0/s");
});

test("throughput is per second and compacts large numbers", () => {
  expect(formatMetricValue(12.5, "records/s")).toBe("12.5/s");
  expect(formatMetricValue(1.234, "records/s")).toBe("1.23/s");
  expect(formatMetricValue(15300, "records/s")).toBe("15.3k/s");
  expect(formatMetricValue(2_500_000, "count")).toBe("2.5M");
});

test("latency switches from ms to seconds", () => {
  expect(formatMetricValue(3.25, "ms")).toBe("3.3 ms");
  expect(formatMetricValue(42.4, "ms")).toBe("42 ms");
  expect(formatMetricValue(1500, "ms")).toBe("1.50 s");
});

test("ratios render as percentages; task counts as integers", () => {
  expect(formatMetricValue(1, "ratio")).toBe("100%");
  expect(formatMetricValue(0.873, "ratio")).toBe("87.3%");
  expect(formatMetricValue(0, "ratio")).toBe("0%");
  expect(formatMetricValue(3, "tasks")).toBe("3");
});

test("the range picker offers exactly what the backend accepts", () => {
  expect(METRIC_RANGES.map((r) => r.id)).toEqual(["15m", "1h", "6h", "24h"]);
});

// ── tiles, chart geometry and hover lookup ────────────────────────────────────────────────
import {
  PREVIEW_ERROR_DOWN, PREVIEW_ERROR_OFF, WIDE_METRICS, chartGeometry, metricTone, nearestPoint,
} from "../../src/connectorMetrics";
import type { ConnectorMetrics, ConnectorMetricsSummary } from "../../src/types/api";

const pts = (vals: (number | null)[]) => vals.map((v, i) => ({ t: 1000 + i * 60, v }));

test("throughput and lag tiles are the wide ones", () => {
  expect([...WIDE_METRICS].sort()).toEqual(["sink_consumer_lag", "sink_read_rate", "sink_send_rate", "source_poll_rate", "source_write_rate"]);
});

test("a tile is only coloured when its own number says so, and a missing value is never coloured", () => {
  expect(metricTone("running_ratio", 0.5)).toBe("bad");
  expect(metricTone("running_ratio", 1)).toBe("");
  expect(metricTone("record_error_rate", 2.4)).toBe("warn");
  expect(metricTone("record_error_rate", 0.2)).toBe("");
  expect(metricTone("sink_consumer_lag", 20000)).toBe("warn");
  expect(metricTone("sink_consumer_lag", 100)).toBe("");
  expect(metricTone("source_poll_rate", 99999)).toBe("");
  for (const missing of [null, undefined, NaN]) expect(metricTone("running_ratio", missing as number | null)).toBe("");
});

test("chartGeometry needs at least two points and one real sample, otherwise there is nothing to draw", () => {
  expect(chartGeometry([], "records/s", false)).toBeNull();
  expect(chartGeometry(pts([5]), "records/s", false)).toBeNull();
  expect(chartGeometry(pts([null, null, null]), "records/s", false)).toBeNull();
});

test("chartGeometry reads throughput against zero, keeps the peak inside the box, and breaks the line at a missing sample", () => {
  const g = chartGeometry(pts([10, 20, null, 40]), "records/s", false)!;
  expect(g.height).toBe(64);
  expect(g.y(0)).toBeGreaterThan(g.y(40));                       // higher values are higher up (smaller y)
  expect(g.y(40)).toBeGreaterThanOrEqual(0);                      // headroom: the peak is not clipped
  expect(g.line.match(/M/g)).toHaveLength(2);                     // 10→20, gap, then 40 starts a new segment
  expect(g.last).toEqual({ t: 1000 + 3 * 60, v: 40 });
  expect(g.grid).toEqual([]);                                     // no axis grid on a small tile
});

test("chartGeometry draws a larger chart with an axis grid when a tile is enlarged", () => {
  const g = chartGeometry(pts([1, 2, 3]), "count", true)!;
  expect(g.height).toBe(200);
  expect(g.grid).toHaveLength(4);
  expect(g.grid[3]!.v).toBeGreaterThan(g.grid[0]!.v);
});

test("ratios are always drawn on a 0..1 scale and milliseconds are not forced down to zero", () => {
  const ratio = chartGeometry(pts([0.9, 0.95, 1]), "ratio", false)!;
  expect(ratio.y(0)).toBeGreaterThan(ratio.y(1));
  const ms = chartGeometry(pts([100, 110, 120]), "ms", false)!;
  expect(ms.y(100)).toBeGreaterThan(ms.y(120));
  expect(ms.y(100)).toBeLessThanOrEqual(64);                      // 100 is near the bottom, not at an axis that starts at 0
});

test("a flat series still draws (a constant line, not a division by zero)", () => {
  const g = chartGeometry(pts([5, 5, 5]), "records/s", false)!;
  expect(g.line).not.toContain("NaN");
  expect(g.area).not.toContain("NaN");
});

test("nearestPoint finds the sample under the cursor and clamps outside the chart", () => {
  const p = pts([1, 2, 3, 4]);
  expect(nearestPoint(p, 0)!.v).toBe(1);
  expect(nearestPoint(p, 1)!.v).toBe(4);
  expect(nearestPoint(p, 0.5)!.v).toBe(2);                         // 1.5 samples in: the closer of 2 and 3 (ties: the earlier)
  expect(nearestPoint(p, -3)!.v).toBe(1);
  expect(nearestPoint(p, 9)!.v).toBe(4);
  expect(nearestPoint([], 0.5)).toBeNull();
});

const metrics = (): ConnectorMetrics => ({
  connector_name: "c", pipeline_id: "p", pipeline_name: "n", direction: "source", runtime_pool_id: "connect-cdc-pool",
  range: "1h", step_seconds: 60, observed_at: "2026-10-08T00:00:00Z", prometheus_enabled: true, reachable: true, error: null,
  metrics: [{ id: "source_poll_rate", label: "Records polled", unit: "records/s", description: "", current: 12.5, series: pts([10, 12.5]) }],
  task_columns: [], tasks: [{ task: "0", status: "running", worker: "w", values: { a: 1 } }],
});

test("a series that stops reporting keeps a dashed 'last known value' line to the right edge; gaps in the middle stay gaps", () => {
  const series = (vs: (number | null)[]) => vs.map((v, i) => ({ t: 1000 + i * 60, v }));
  const trailing = chartGeometry(series([369, 0, 0, 0, null, null, null]), "count", false)!;
  expect(trailing.hold).toMatch(/^M[\d.]+,[\d.]+L600,[\d.]+$/);                  // from the last sample to the right edge…
  expect(trailing.hold.split("L")[0]!.split(",")[1]).toBe(trailing.hold.split("L")[1]!.split(",")[1]);   // …perfectly flat
  expect(trailing.line).not.toContain("L600");                                     // the solid line still stops where data stops

  expect(chartGeometry(series([1, 2, 3, 4]), "count", false)!.hold).toBe("");       // reaches the right edge: nothing to hold
  const middle = chartGeometry(series([5, null, 7, 8]), "count", false)!;
  expect(middle.hold).toBe("");                                                     // a gap in the middle is not bridged
  expect(middle.line.match(/M/g)).toHaveLength(2);
});
