/** Alerts: the words and the numbers the screens use (they must agree with what the backend accepts). */
import { describe, expect, it } from "vitest";
import { JOB_SECTION, KIND_USES, RULE_KIND_ORDER, WINDOWS, describeRule, windowText } from "../../src/features/alerts/kinds";
import { formatTs, showing } from "../../src/features/alerts/format";

const rule = (over: Record<string, unknown>) => ({ kind: "run_failed", threshold: 1, min_runs: 1, window_seconds: 3600, ...over }) as Parameters<typeof describeRule>[0];

describe("describeRule", () => {
  it("reads each kind as a short sentence", () => {
    expect(describeRule(rule({ kind: "run_failed", threshold: 1 }))).toBe("≥ 1 failed run · 1 hour");
    expect(describeRule(rule({ kind: "run_failed", threshold: 3 }))).toBe("≥ 3 failed runs · 1 hour");
    expect(describeRule(rule({ kind: "consecutive_failures", threshold: 3, window_seconds: 0 }))).toBe("3 runs in a row failed");
    expect(describeRule(rule({ kind: "failure_rate", threshold: 50, min_runs: 5, window_seconds: 0 }))).toBe("≥ 50% failed · min 5 runs · 24 hours");
    expect(describeRule(rule({ kind: "failure_rate", threshold: 20, min_runs: 1, window_seconds: 604800 }))).toBe("≥ 20% failed · min 1 run · 7 days");
    expect(describeRule(rule({ kind: "quality_failed", threshold: 1, window_seconds: 86400 }))).toBe("≥ 1 failed quality run · 24 hours");
    expect(describeRule(rule({ kind: "upstream_blocked", threshold: 2, window_seconds: 900 }))).toBe("≥ 2 refused runs · 15 minutes");
    expect(describeRule(rule({ kind: "flink_job_failed", threshold: 1, window_seconds: 0 }))).toBe("while the job is failed");
  });
});

describe("windows", () => {
  it("offers exactly the windows the backend accepts, and 0 reads as the 24 hour default", () => {
    expect(WINDOWS.map((w) => w.value)).toEqual([0, 300, 900, 3600, 21600, 86400, 604800]);
    expect(windowText(0)).toBe("24 hours");
    expect(windowText(300)).toBe("5 minutes");
    expect(windowText(12345)).toBe("12345 s");
  });
});

describe("rule kinds", () => {
  it("every kind has the numbers it uses, and Flink's rule has no window or threshold to choose", () => {
    expect(RULE_KIND_ORDER).toHaveLength(6);
    for (const k of RULE_KIND_ORDER) expect(KIND_USES[k]).toBeTruthy();
    expect(KIND_USES.failure_rate).toEqual({ window: true, minRuns: true, maxThreshold: 100 });
    expect(KIND_USES.flink_job_failed).toEqual({ window: false, minRuns: false, maxThreshold: 1 });
  });

  it("a job opens on the page of its own kind", () => {
    expect(JOB_SECTION).toEqual({ dbt: "dbt", sql: "sqljobs", python: "pythonjobs", flink: "flink" });
  });
});

describe("format", () => {
  it("shows a timestamp as date and minutes, and a dash for nothing", () => {
    expect(formatTs("2026-10-29T09:42:00Z")).toMatch(/^2026-10-29 \d\d:\d\d$/);
    expect(formatTs(null)).toBe("—");
    expect(formatTs("not a date")).toBe("—");
  });

  it("says which rows are shown", () => {
    expect(showing(0, 10, 34, "alert")).toBe("Showing 1–10 of 34 alerts");
    expect(showing(30, 4, 34, "alert")).toBe("Showing 31–34 of 34 alerts");
    expect(showing(0, 1, 1, "rule")).toBe("Showing 1–1 of 1 rule");
    expect(showing(0, 0, 0, "channel")).toBe("No channels");
  });
});
