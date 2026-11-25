/** Labels, colours and shared styles for the Data Quality surface. */
import type { CSSProperties } from "react";
import type { DqDimension, DqEnforcementMode, DqJobType, DqRuleType, DqSeverity } from "../../types";

export const JOB_TYPES: { value: DqJobType; label: string }[] = [
  { value: "dbt_project", label: "dbt project" },
  { value: "flink_job", label: "FlinkSQL job" },
  { value: "python_job", label: "Python job" },
];

export const jobTypeLabel = (t: DqJobType): string => JOB_TYPES.find((j) => j.value === t)?.label ?? t;

// Enforcement is the one field with real operational consequence, so each
// option says what it does to the job rather than just naming itself.
export const MODES: { value: DqEnforcementMode; label: string; hint: string }[] = [
  { value: "blocking", label: "Blocking", hint: "A failed check fails the job." },
  { value: "warn", label: "Warn", hint: "Violations are recorded; the job still succeeds." },
  { value: "async", label: "Async", hint: "Checks run as a separate job, out of band." },
];

export const JOB_TYPE_STYLE: Record<DqJobType, { icon: string; fg: string; bg: string }> = {
  dbt_project: { icon: "ti-brand-databricks", fg: "#2F6B14", bg: "#eaf3e1" },
  flink_job: { icon: "ti-wave-sine", fg: "#1e40af", bg: "#E3F6EA" },
  python_job: { icon: "ti-brand-python", fg: "#6b21a8", bg: "#f3e8ff" },
};

export const MODE_STYLE: Record<DqEnforcementMode, { fg: string; bg: string }> = {
  blocking: { fg: "#b23b3b", bg: "#fbe9e9" },
  warn: { fg: "#a8470a", bg: "#fdebdd" },
  async: { fg: "#4b5a53", bg: "#eef2ed" },
};

export const chip = (fg: string, bg: string): CSSProperties => ({
  background: bg, color: fg, padding: "2px 8px", borderRadius: 4,
  fontSize: 11.5, fontWeight: 500, display: "inline-flex", alignItems: "center",
  gap: 4, whiteSpace: "nowrap",
});

export const cellEllipsis: CSSProperties = {
  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
};

export const codeBlock: CSSProperties = {
  margin: 0, padding: "10px 12px", background: "#F8FAF9",
  border: "0.5px solid #E3E8E2", borderRadius: 6, fontSize: 11.5,
  fontFamily: "monospace", lineHeight: 1.55, overflow: "auto",
  whiteSpace: "pre", color: "#14201B",
};

// ── Rule library / rulesets ──────────────────────────────────────────────────

export const DIMENSIONS: DqDimension[] = ["nulls", "uniqueness", "validity", "freshness", "volume", "schema", "custom"];
export const SEVERITIES: DqSeverity[] = ["CRITICAL", "ERROR", "WARNING", "INFO"];
export const RULE_TYPES: DqRuleType[] = [
  "not_null", "unique", "accepted_values", "range", "regex",
  "freshness", "row_count", "row_count_anomaly", "custom_sql", "custom_python",
];

const GRAY = { fg: "#4b5a53", bg: "#eef2ed" };
export const dimStyle = (d?: string) =>
  d === "nulls" ? { fg: "#b23b3b", bg: "#fbe9e9" }
    : d === "uniqueness" ? { fg: "#0f766e", bg: "#ccfbf1" }
      : d === "validity" ? { fg: "#2F6B14", bg: "#eaf3e1" }
        : d === "freshness" || d === "volume" ? { fg: "#a8470a", bg: "#fdebdd" }
          : d === "schema" ? { fg: "#6b21a8", bg: "#f3e8ff" } : GRAY;
export const sevStyle = (s?: string) =>
  s === "CRITICAL" || s === "ERROR" ? { fg: "#b23b3b", bg: "#fbe9e9" }
    : s === "WARNING" ? { fg: "#a8470a", bg: "#fdebdd" } : GRAY;

/** Grid header row shared by the DQ tables. */
export const tableHead = (cols: string): CSSProperties => ({
  display: "grid", gridTemplateColumns: cols, gap: 12, padding: "6px 16px", fontSize: 10.5, color: "#73827a",
  textTransform: "uppercase", letterSpacing: ".06em", borderBottom: "0.5px solid #E3E8E2", background: "#F8FAF9",
});
export const tableRow = (cols: string): CSSProperties => ({
  display: "grid", gridTemplateColumns: cols, gap: 12, alignItems: "center", padding: "9px 16px",
  fontSize: 12.5, borderBottom: "0.5px solid #f0f0ed",
});

/** "{ … }" text → object, or null when it isn't a JSON object. */
export const parseJsonObject = (s: string): Record<string, unknown> | null => {
  try {
    const v = JSON.parse(s.trim() || "{}");
    return v && typeof v === "object" && !Array.isArray(v) ? v : null;
  } catch { return null; }
};
