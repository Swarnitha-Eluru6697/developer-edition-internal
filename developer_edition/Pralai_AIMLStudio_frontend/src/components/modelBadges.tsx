import type { BaseModel, Domain, ModelKind } from "../types/domain";

/**
 * Deep learning is a FRAMEWORK fact, not a `domain` value — domain stays ml/llm,
 * matching the URL and badge conventions everything else is built on. Every
 * genuinely deep-learning entry in the library (CNN, GRU/LSTM, autoencoders,
 * the deep MLP, tabular-embedding-nn, TFT, N-BEATS) is framework "pytorch",
 * while classical tabular methods (xgboost, sklearn's own MLP, lightgbm, …) are
 * not.
 *
 * Defined once here and used by BOTH the badge and the catalog's
 * "Deep Learning" filter, so a model cannot be filtered into that tab and then
 * badged as plain ML — which is exactly what happened while the filter knew
 * about pytorch and the badge didn't.
 */
export function isDeepLearning(framework: string | null | undefined): boolean {
  return framework === "pytorch";
}

/**
 * ML / DL / LLM pill — the same badge everywhere a model's domain is shown.
 *
 * `framework` is optional only so a caller that genuinely has no framework to
 * hand still renders something; pass it wherever it exists, because omitting it
 * silently downgrades a deep-learning model to "ML".
 */
export function domainBadge(domain: Domain, framework?: string | null) {
  if (domain === "llm") return <span className="badge badge-llm"><span className="dot" />LLM</span>;
  if (isDeepLearning(framework)) return <span className="badge badge-dl"><span className="dot" />DL</span>;
  return <span className="badge badge-ml"><span className="dot" />ML</span>;
}

/**
 * How a model came to exist. `trained` is the ordinary case and gets no badge —
 * the interesting distinction is a derived LLM: a full fine-tune versus a LoRA
 * adapter, which are deployed very differently (its own weights vs. riding on a
 * shared multi-LoRA base deployment).
 */
export function kindBadge(kind: ModelKind) {
  if (kind === "trained") return null;
  return <span className="badge badge-outline">{kind === "adapter" ? "adapter" : "derived_full"}</span>;
}

export const CATEGORY_LABELS: Record<string, string> = {
  classification: "Classification",
  regression: "Regression",
  clustering: "Clustering",
  anomaly_detection: "Anomaly Detection",
  timeseries: "Time-Series Forecasting",
  dimensionality_reduction: "Dimensionality Reduction",
};

/**
 * The catalog's and the Train wizard's domain filter, off the same
 * `isDeepLearning` definition the badge uses.
 */
export function matchesDomainView(b: Pick<BaseModel, "domain" | "framework">, view: string): boolean {
  if (view === "all") return true;
  if (view === "deep-learning") return isDeepLearning(b.framework);
  if (view === "ml") return b.domain === "ml" && !isDeepLearning(b.framework);
  return b.domain === view;
}
