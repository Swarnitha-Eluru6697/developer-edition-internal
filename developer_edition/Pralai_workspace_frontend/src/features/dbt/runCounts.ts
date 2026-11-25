/** Wording for a run's failed-model count, shared by the project list and the run history so
 *  both say the same thing for the same run: "1/13 models failed". Returns null when nothing
 *  failed — callers then keep their usual text for a run that did not fail ("55 models" in the
 *  list, "13/13 models" in the history).
 */
export interface RunCounts {
  models_total?: number | null;
  models_failed?: number | null;
}

/** What one counted step of a run is: a dbt model, a statement of a SQL job, or a step of a Python job. */
export type RunUnit = "model" | "statement" | "step";

export const plural = (n: number, unit: RunUnit) => `${n} ${unit}${n === 1 ? "" : "s"}`;

export function failedModelsText(run: RunCounts, unit: RunUnit = "model"): string | null {
  const failed = run.models_failed ?? 0;
  if (run.models_total == null || failed <= 0) return null;
  return `${failed}/${run.models_total} ${unit}s failed`;
}
