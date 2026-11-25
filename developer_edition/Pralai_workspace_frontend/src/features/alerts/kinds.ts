/** Words for the alerts screen: rule kinds, windows, severities, job kinds, and how a rule's numbers read in a sentence. */
import type { AlertRule, AlertRuleKind, AlertSeverity } from "../../types";

export const RULE_KIND_LABEL: Record<AlertRuleKind, string> = {
  run_failed: "run failed",
  consecutive_failures: "repeated failures",
  failure_rate: "failure rate",
  quality_failed: "quality check failed",
  upstream_blocked: "upstream blocked",
  flink_job_failed: "flink job failed",
};

export const RULE_KIND_ORDER: AlertRuleKind[] = [
  "run_failed", "consecutive_failures", "failure_rate", "quality_failed", "upstream_blocked", "flink_job_failed",
];

export const SEVERITIES: AlertSeverity[] = ["low", "medium", "high"];

export const SEVERITY_FLOOR: Record<AlertSeverity, string> = {
  low: "Low and above — everything",
  medium: "Medium and above — skips low-severity notices",
  high: "High only — just the ones worth waking someone for",
};
export const SEVERITY_FLOOR_SHORT: Record<AlertSeverity, string> = { low: "low and above", medium: "medium and above", high: "high only" };

/** The windows the backend accepts (0 = its default, 24 hours). */
export const WINDOWS: { value: number; label: string }[] = [
  { value: 0, label: "Default (24 hours)" },
  { value: 300, label: "5 minutes" },
  { value: 900, label: "15 minutes" },
  { value: 3600, label: "1 hour" },
  { value: 21600, label: "6 hours" },
  { value: 86400, label: "24 hours" },
  { value: 604800, label: "7 days" },
];
const WINDOW_TEXT = new Map(WINDOWS.map((w) => [w.value, w.value === 0 ? "24 hours" : w.label]));
export const windowText = (seconds: number): string => WINDOW_TEXT.get(seconds) ?? `${seconds} s`;

export const JOB_KIND_LABEL: Record<string, string> = { dbt: "dbt", sql: "SQL", python: "Python", flink: "Flink" };

/** Where a job of each kind lives in the sidebar (the page a "Open job" button goes to). */
export const JOB_SECTION = { dbt: "dbt", sql: "sqljobs", python: "pythonjobs", flink: "flink" } as const;

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** "≥ 3 failed runs · 1 hour" — what makes a rule fire, in a few words (the Rules table's "Fires when" column). */
export function describeRule(r: Pick<AlertRule, "kind" | "threshold" | "min_runs" | "window_seconds">): string {
  const win = windowText(r.window_seconds);
  switch (r.kind) {
    case "run_failed": return `≥ ${plural(r.threshold, "failed run")} · ${win}`;
    case "consecutive_failures": return `${plural(r.threshold, "run")} in a row failed`;
    case "failure_rate": return `≥ ${r.threshold}% failed · min ${plural(r.min_runs, "run")} · ${win}`;
    case "quality_failed": return `≥ ${plural(r.threshold, "failed quality run")} · ${win}`;
    case "upstream_blocked": return `≥ ${plural(r.threshold, "refused run")} · ${win}`;
    case "flink_job_failed": return "while the job is failed";
  }
}

/** The numbers a rule kind uses (mirrors the backend's RULE_KINDS; the form also reads them from /alerts/rules/kinds). */
export const KIND_USES: Record<AlertRuleKind, { window: boolean; minRuns: boolean; maxThreshold: number }> = {
  run_failed: { window: true, minRuns: false, maxThreshold: 1000 },
  consecutive_failures: { window: true, minRuns: false, maxThreshold: 100 },
  failure_rate: { window: true, minRuns: true, maxThreshold: 100 },
  quality_failed: { window: true, minRuns: false, maxThreshold: 1000 },
  upstream_blocked: { window: true, minRuns: false, maxThreshold: 1000 },
  flink_job_failed: { window: false, minRuns: false, maxThreshold: 1 },
};
