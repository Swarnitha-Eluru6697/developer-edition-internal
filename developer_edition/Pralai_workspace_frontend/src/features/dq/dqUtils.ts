/** Pure helpers for the Data Quality surface. */
import { asApiError } from "../../api/errors";
import type { DqRender } from "../../types";

/** A table name without quotes or its schema/catalog prefix. */
export const leafName = (t: string) => t.replace(/["`]/g, "").split(".").pop() ?? t;

/** Sentinel option for the "type a name" escape hatch in the target-table select. */
export const OTHER_TABLE = "__other__";

/** Human message for a DQ error: 422 refusals carry {reason, error,
 *  remediation, unsupported_rules}; 502 runner failures {error|message};
 *  DQ service errors {detail: {code, message}}. */
export function dqErrorMessage(e: unknown, fallback: string): string {
  const err = asApiError(e);
  const d = err.data ?? {};
  const detail = d.detail && typeof d.detail === "object" ? d.detail.message : undefined;
  const nestedError = d.error && typeof d.error === "object" ? d.error.message : undefined;
  const base = d.error ?? d.message ?? detail ?? nestedError ?? fallback;
  const parts = [typeof base === "string" ? base : fallback];
  const unsupported = d.unsupported_rules;
  if (Array.isArray(unsupported) && unsupported.length) {
    parts.push(`Unsupported: ${unsupported.map((u: { rule_name?: string; rule_type?: string } | string) =>
      typeof u === "object" && u !== null ? u.rule_name ?? u.rule_type ?? u : u).join(", ")}.`);
  }
  if (d.remediation) parts.push(String(d.remediation));
  if (err.status === 502) parts.unshift("The runner couldn't apply it —");
  return parts.join(" ");
}

export interface RenderedArtifact { label: string; hint: string; body: string }

/** The populated artifacts of a render — only those matching the binding's job_type come back. */
export function renderArtifacts(render: DqRender | undefined): RenderedArtifact[] {
  const artifacts: RenderedArtifact[] = [];
  if (render?.dbt_artifact) {
    artifacts.push({
      label: "dbt schema.yml tests",
      hint: "Written to models/_dq_generated_tests.yml in the cloned project before every dbt build.",
      body: render.dbt_artifact,
    });
  }
  if (render?.flinksql_artifact) {
    artifacts.push({
      label: "FlinkSQL quality job",
      hint: "Submitted as a parallel job (job_kind=quality); violations land on quality.violations and are forwarded to the catalog.",
      body: render.flinksql_artifact,
    });
  }
  if (render?.gx_artifact) {
    artifacts.push({
      label: "Great Expectations suite",
      hint: "JSON ExpectationSuite for Python jobs.",
      body: JSON.stringify(render.gx_artifact, null, 2),
    });
  }
  if (render?.soda_artifact) {
    artifacts.push({
      label: "Soda checks",
      hint: "YAML checks file for Python jobs.",
      body: render.soda_artifact,
    });
  }
  return artifacts;
}
