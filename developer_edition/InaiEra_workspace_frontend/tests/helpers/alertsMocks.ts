/** Shared fixtures for the alerts screen tests: rows shaped like the API's, and a fake alerts API the components read from. */
import { vi } from "vitest";
import type { Alert, AlertChannel, AlertRule, AlertRuleKindInfo } from "../../src/types";

export const alert = (over: Partial<Alert> = {}): Alert => ({
  id: "a1", rule_id: "r1", rule_name: "job-run-failed", kind: "run_failed", severity: "high", job_key: "j1", job_name: "customers", job_kind: "dbt",
  summary: "dbt project 'customers' failed: Compilation Error", state: "open", delivery: "sent", raised_at: "2026-10-29T09:42:00Z", sent_at: "2026-10-29T09:42:05Z",
  resolved_at: null, last_action: "sent to #oncall", last_actor: "system", last_at: "2026-10-29T09:42:05Z", ...over,
});

export const rule = (over: Partial<AlertRule> = {}): AlertRule => ({
  id: "r1", name: "job-run-failed", kind: "run_failed", severity: "high", threshold: 1, window_seconds: 3600, min_runs: 1, scope_job_key: null,
  scope_job_name: null, enabled: true, created_by: "system", created_at: "2026-10-29T00:00:00Z", updated_at: "2026-10-29T00:00:00Z", ...over,
});

export const channel = (over: Partial<AlertChannel> = {}): AlertChannel => ({
  id: "c1", name: "#oncall", kind: "slack", url_hint: "hooks.slack.com/…/x7Q2", min_severity: "low", scope_job_key: null, scope_job_name: null, enabled: true,
  last_attempt_at: "2026-10-29T09:42:00Z", last_ok: true, last_detail: "delivered", created_by: "padmin", created_at: "2026-10-29T00:00:00Z",
  updated_at: "2026-10-29T00:00:00Z", ...over,
});

export const KINDS: AlertRuleKindInfo[] = [
  { kind: "run_failed", label: "Run failed", description: "A run ended failed.", threshold_hint: "Number of failed runs in the window. 1 = every failure.", uses_window: true, uses_min_runs: false, max_threshold: 1000 },
  { kind: "consecutive_failures", label: "Repeated failures", description: "The last runs all failed.", threshold_hint: "How many runs in a row.", uses_window: true, uses_min_runs: false, max_threshold: 100 },
  { kind: "failure_rate", label: "Failure rate", description: "Too many runs failed.", threshold_hint: "Percent of runs failed (1-100).", uses_window: true, uses_min_runs: true, max_threshold: 100 },
  { kind: "quality_failed", label: "Quality check failed", description: "A data test failed.", threshold_hint: "Number of such runs.", uses_window: true, uses_min_runs: false, max_threshold: 1000 },
  { kind: "upstream_blocked", label: "Upstream blocked", description: "Dagster refused a run.", threshold_hint: "Number of refused runs.", uses_window: true, uses_min_runs: false, max_threshold: 1000 },
  { kind: "flink_job_failed", label: "Flink job failed", description: "A Flink job failed.", threshold_hint: "Not used.", uses_window: false, uses_min_runs: false, max_threshold: 1 },
];

/** A fake RTK mutation hook: [trigger, { isLoading }]; the trigger records its argument and resolves with `result`. */
export function mutation<A, R>(calls: unknown[], result: R | ((a: A) => R | Promise<R>), failWith?: () => unknown) {
  return () => [
    vi.fn((arg: A) => {
      calls.push(arg);
      return {
        unwrap: async () => {
          const err = failWith?.();
          if (err) throw err;
          return typeof result === "function" ? (result as (a: A) => R)(arg) : result;
        },
      };
    }),
    { isLoading: false, originalArgs: calls[calls.length - 1] },
  ];
}
