/*
 * Words for the Alerts page: rule kinds, windows, severities, and how a rule's numbers read in a sentence. The backend's own descriptions
 * (GET /alerts/rules/kinds) are shown where there is room; these short forms fill the tables and cards. Ported from the Workspace's
 * features/alerts/kinds.ts, for DataConnect's pipeline rules (app/services/alerts/kinds.py).
 */
import type { AlertRule, AlertRuleKind, AlertSeverity, AlertState } from "./types/api";

export const RULE_KIND_LABEL: Record<AlertRuleKind, string> = {
  pipeline_failed: "pipeline failed",
  pipeline_degraded: "pipeline degraded",
  operation_failed: "deploy or action failed",
  error_threshold_paused: "paused by error threshold",
  dlq_errors: "dead-letter errors",
  quality_failures: "quality check failures",
  runtime_unreachable: "connector state unknown",
  schema_drift_pending: "schema change waiting",
};

export const RULE_KIND_ORDER: AlertRuleKind[] = [
  "pipeline_failed", "pipeline_degraded", "operation_failed", "error_threshold_paused", "dlq_errors", "quality_failures",
  "runtime_unreachable", "schema_drift_pending",
];

export const SEVERITIES: AlertSeverity[] = ["low", "medium", "high"];

export const SEVERITY_FLOOR: Record<AlertSeverity, string> = {
  low: "Low and above — everything",
  medium: "Medium and above — skips low-severity notices",
  high: "High only — just the ones worth waking someone for",
};
export const SEVERITY_FLOOR_SHORT: Record<AlertSeverity, string> = { low: "low and above", medium: "medium and above", high: "high only" };

/** The windows the backend accepts (0 = its default, 1 hour). */
export const WINDOWS: { value: number; label: string }[] = [
  { value: 0, label: "Default (1 hour)" },
  { value: 300, label: "5 minutes" },
  { value: 900, label: "15 minutes" },
  { value: 3600, label: "1 hour" },
  { value: 21600, label: "6 hours" },
  { value: 86400, label: "24 hours" },
  { value: 604800, label: "7 days" },
];
const WINDOW_TEXT = new Map(WINDOWS.map((w) => [w.value, w.value === 0 ? "1 hour" : w.label]));
export const windowText = (seconds: number): string => WINDOW_TEXT.get(seconds) ?? `${seconds} s`;

/** The numbers each kind uses (mirrors the backend's RULE_KINDS; the form also reads /alerts/rules/kinds). */
export const KIND_USES: Record<AlertRuleKind, { window: boolean; maxThreshold: number; fixed: boolean }> = {
  pipeline_failed: { window: false, maxThreshold: 1, fixed: true },
  pipeline_degraded: { window: false, maxThreshold: 1, fixed: true },
  operation_failed: { window: true, maxThreshold: 1000, fixed: false },
  error_threshold_paused: { window: true, maxThreshold: 1000, fixed: false },
  dlq_errors: { window: true, maxThreshold: 1_000_000, fixed: false },
  quality_failures: { window: true, maxThreshold: 1_000_000, fixed: false },
  runtime_unreachable: { window: false, maxThreshold: 1440, fixed: false },
  schema_drift_pending: { window: false, maxThreshold: 720, fixed: false },
};

const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

/** "≥ 100 dead-letter records · 1 hour" — what makes a rule fire, in a few words (the Rules table's "Fires when" column). */
export function describeRule(r: Pick<AlertRule, "kind" | "threshold" | "window_seconds">): string {
  const win = windowText(r.window_seconds);
  switch (r.kind) {
    case "pipeline_failed": return "while the pipeline is failed";
    case "pipeline_degraded": return "while the pipeline is degraded";
    case "operation_failed": return `≥ ${plural(r.threshold, "failed action")} · ${win}`;
    case "error_threshold_paused": return `≥ ${plural(r.threshold, "pause")} · ${win}`;
    case "dlq_errors": return `≥ ${plural(r.threshold, "dead-letter record")} · ${win}`;
    case "quality_failures": return `≥ ${plural(r.threshold, "failing record")} · ${win}`;
    case "runtime_unreachable": return `state unknown for ${plural(r.threshold, "minute")}`;
    case "schema_drift_pending": return `waiting ${plural(r.threshold, "hour")} for approval`;
  }
}

export const DIRECTION_LABEL: Record<string, string> = { SOURCE: "source", SINK: "sink" };

export const ALERT_TABS = ["open", "acknowledged", "resolved", "rules", "channels"] as const;
export type AlertTab = (typeof ALERT_TABS)[number];
export const isAlertState = (t: AlertTab): t is AlertState => t === "open" || t === "acknowledged" || t === "resolved";

export const PAGE_SIZE = 10;

const pad = (n: number) => String(n).padStart(2, "0");
/** "2026-10-30 09:42" in the viewer's own time zone. */
export function formatTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "Showing 11–20 of 34 alerts" */
export function showing(offset: number, shown: number, total: number, noun: string): string {
  if (!total) return `No ${noun}s`;
  return `Showing ${offset + 1}–${offset + shown} of ${total} ${noun}${total === 1 ? "" : "s"}`;
}
