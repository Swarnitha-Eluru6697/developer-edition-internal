/*
 * Shared observed-runtime-state vocabulary (FE-BUG-07/FE-BUG-16). The
 * backend's derive_observed_state() (app/services/runtime_reconciler.py) —
 * used by both the reconciler and MonitoringService — only ever produces
 * running/paused/degraded/failed/absent/unknown. This is the one place that
 * vocabulary's labels/colors/"how long ago" formatting live, so
 * Pipelines.tsx's runtime drawer and Monitoring.tsx's connector/pipeline
 * status rendering can't drift into two different mappings for the same
 * backend values.
 */

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const ms = Math.max(0, Date.now() - new Date(iso).getTime());
  if (ms < 60_000) return `${Math.max(1, Math.round(ms / 1000))} sec ago`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)} min ago`;
  if (ms < 86_400_000) return `${Math.round(ms / 3_600_000)} hr ago`;
  return `${Math.round(ms / 86_400_000)} day(s) ago`;
}

const RUNTIME_STATE_LABEL: Record<string, string> = {
  running: "RUNNING", paused: "PAUSED", degraded: "DEGRADED", failed: "FAILED",
  absent: "ABSENT", unknown: "UNKNOWN",
};

// FE-BUG-16: RUNNING / FAILED / DEGRADED / PAUSED / UNKNOWN — the exact
// 5-value vocabulary the ticket calls for. `absent` (no connector exists in
// Kafka Connect at all — e.g. a draft never deployed) renders the same as
// UNKNOWN here: neither the ticket nor this app's UI needs to distinguish
// "never existed" from "can't tell right now" visually.
const RUNTIME_PILL_CLASS: Record<string, string> = {
  running: "bg", paused: "bb", degraded: "ba", failed: "br", absent: "bgr", unknown: "bgr",
};

export function runtimeStateLabel(state: string | null | undefined): string {
  if (!state) return "UNKNOWN";
  return RUNTIME_STATE_LABEL[state] ?? state.toUpperCase();
}

export function runtimePillClass(state: string | null | undefined): string {
  return (state && RUNTIME_PILL_CLASS[state]) || "bgr";
}

/** Mirrors app/services/runtime_reconciler.py's derive_observed_state() for
 * a GET /runtime/flow connector entry — client-side because flow_summary's
 * connectors carry the raw Kafka-Connect state/task strings (RuntimeFlow
 * schema), not the reconciler's own already-derived vocabulary the way
 * ConnectorStatus/PipelineRuntimeState do. Only the "exists, here's its raw
 * status" branch applies here: flow_summary only lists connectors Kafka
 * Connect's own /connectors call actually returned, so there's no separate
 * "absent" case to derive. */
export function deriveFlowConnectorState(connector: { state?: string | null; tasks?: (string | null)[] } | undefined): string {
  if (!connector) return "unknown";
  const state = (connector.state || "UNKNOWN").toUpperCase();
  if (state === "FAILED" || state === "UNASSIGNED") return "failed";
  if ((connector.tasks || []).some((t) => (t || "").toUpperCase() === "FAILED")) return "degraded";
  if (state === "PAUSED") return "paused";
  if (state === "RUNNING") return "running";
  return "unknown";
}
