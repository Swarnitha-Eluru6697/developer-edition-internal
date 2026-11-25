import type { Pool } from "pg";

/**
 * The absolute acceptance gate a training-run candidate has to clear on its own
 * merits — separate from, and in addition to, the regression gate that compares
 * a candidate against the current champion.
 *
 * Two rules govern everything here:
 *
 *  1. The policy is READ, never assumed. `validation_policies` is the platform's
 *     stored bar; if a deployment changes a threshold, this reports the changed
 *     one. Nothing about the numbers is compiled into the UI.
 *
 *  2. A metric the run never recorded is reported as `not-recorded`, and the
 *     overall verdict becomes `incomplete` rather than `passed`. A gate that
 *     silently skips the checks it cannot evaluate and then says "PASSED" is
 *     worse than no gate at all — it is an assurance nobody actually made.
 *     (Runs trained before precision/recall/ROC-AUC were measured land here,
 *     and say so.)
 */

export type GateRowResult = "pass" | "fail" | "not-recorded";

export interface GateRow {
  key: string;
  label: string;
  /** The stored threshold, in the metric's own units. */
  threshold: number;
  direction: "min" | "max";
  scale: "percent" | "unit";
  actual: number | null;
  result: GateRowResult;
}

export interface ValidationGate {
  applicable: true;
  policyId: string;
  taskKind: string;
  description: string;
  rows: GateRow[];
  generalization: {
    label: string;
    maxGap: number;
    trainValue: number | null;
    validationValue: number | null;
    gap: number | null;
    result: GateRowResult;
  } | null;
  verdict: { status: "passed" | "failed" | "incomplete"; message: string };
}

export interface ValidationGateNotApplicable {
  applicable: false;
  reason: string;
}

interface PolicyMetric {
  key: string;
  label: string;
  threshold: number;
  direction: "min" | "max";
  scale: "percent" | "unit";
}

/** Which stored policy grades this run, decided by the metric vocabulary the
 *  trainer actually reported — never by the base model's declared task, which
 *  can disagree with what was really run (a ranking model trained as a
 *  regressor reports r2, not NDCG). */
function taskKindFor(metrics: Record<string, unknown>): string | null {
  if (typeof metrics.accuracy === "number") return "classification";
  if (typeof metrics.r2 === "number") return "regression";
  return null;
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export async function evaluateValidationGate(
  pool: Pool,
  run: { status: string; metrics: Record<string, unknown> | null }
): Promise<ValidationGate | ValidationGateNotApplicable> {
  if (run.status !== "succeeded") {
    return {
      applicable: false,
      reason: `This run is ${run.status} — there is no candidate to grade yet. The acceptance gate runs against a completed run's measured test-split metrics.`,
    };
  }
  const metrics = run.metrics ?? {};
  const taskKind = taskKindFor(metrics);
  if (!taskKind) {
    return {
      applicable: false,
      reason:
        "No acceptance policy covers this run's metric vocabulary. Policies are defined for classification (accuracy/precision/recall/F1/ROC-AUC) and regression (R²); clustering, anomaly-detection, forecasting and dimensionality-reduction runs report different measures and are not graded against one.",
    };
  }

  const policyResult = await pool.query(
    "SELECT * FROM validation_policies WHERE task_kind = $1 ORDER BY id LIMIT 1",
    [taskKind]
  );
  if (policyResult.rows.length === 0) {
    return { applicable: false, reason: `No validation policy is stored for '${taskKind}' runs.` };
  }
  const policy = policyResult.rows[0];
  const policyMetrics: PolicyMetric[] = policy.metrics ?? [];

  const rows: GateRow[] = policyMetrics.map((m) => {
    const actual = num(metrics[m.key]);
    let result: GateRowResult = "not-recorded";
    if (actual !== null) {
      result = (m.direction === "min" ? actual >= m.threshold : actual <= m.threshold) ? "pass" : "fail";
    }
    return { key: m.key, label: m.label, threshold: m.threshold, direction: m.direction, scale: m.scale, actual, result };
  });

  // Overfitting is only measurable when the trainer recorded BOTH sides of the
  // split. train − validation on the same metric, in that metric's own units.
  let generalization: ValidationGate["generalization"] = null;
  const maxGap = num(policy.max_generalization_gap);
  if (maxGap !== null) {
    const pair =
      taskKind === "classification"
        ? { label: "Overfitting (train − validation accuracy)", train: num(metrics.trainAccuracy), val: num(metrics.accuracy) }
        : { label: "Overfitting (train − validation R²)", train: num(metrics.trainR2), val: num(metrics.r2) };
    const gap = pair.train !== null && pair.val !== null ? Number((pair.train - pair.val).toFixed(4)) : null;
    generalization = {
      label: pair.label,
      maxGap,
      trainValue: pair.train,
      validationValue: pair.val,
      gap,
      result: gap === null ? "not-recorded" : gap <= maxGap ? "pass" : "fail",
    };
  }

  const all: GateRowResult[] = [...rows.map((r) => r.result), ...(generalization ? [generalization.result] : [])];
  const failed = rows.filter((r) => r.result === "fail");
  const missing = all.filter((r) => r === "not-recorded").length;

  let verdict: ValidationGate["verdict"];
  if (all.includes("fail")) {
    const worst = failed[0];
    verdict = {
      status: "failed",
      message: worst
        ? `${worst.label} required ${worst.direction === "min" ? "≥" : "≤"} ${worst.threshold}, actual ${worst.actual}. This candidate can't be promoted on this policy; adjust and retrain.`
        : `The generalization gap exceeded the policy's maximum of ${generalization?.maxGap}. This candidate is overfit relative to the bar; adjust and retrain.`,
    };
  } else if (missing > 0) {
    verdict = {
      status: "incomplete",
      message: `${all.length - missing} of ${all.length} checks passed and ${missing} could not be evaluated — this run never recorded those metrics. It is not a pass: the unevaluated checks are unknown, not satisfied. Retrain to record them.`,
    };
  } else {
    verdict = {
      status: "passed",
      message: "Candidate clears every acceptance threshold in this policy and can proceed to registration and promotion.",
    };
  }

  return {
    applicable: true,
    policyId: policy.id,
    taskKind,
    description: policy.description,
    rows,
    generalization,
    verdict,
  };
}
