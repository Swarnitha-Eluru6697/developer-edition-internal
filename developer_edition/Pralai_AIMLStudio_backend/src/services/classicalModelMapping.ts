// Resolves a base model to the real Python class classical_trainer.py
// should actually fit, and which task-shaped fit/score path to use.
// Covers 60 of ML Studio's 78 non-LLM base models — see classical_trainer.py's
// own module docstring for exactly which 6 are explicitly NOT covered yet
// (Prophet/ARIMA/SARIMA/auto-ARIMA/exponential-smoothing/Holt-Winters —
// true time-series libraries with a genuinely different fit shape).

// The 9 base models whose `uri` is an oci:// reference (a training-
// container image reference, not a Python import path) but whose actual
// library exposes a real sklearn-compatible fit/predict or fit_transform
// API — resolved here rather than by changing what `uri` means for these
// entries, which should keep meaning "where the source/container
// reference lives," not "what class this scaffold happens to use as a
// local stand-in."
const NON_SKLEARN_CLASSPATH: Record<string, string> = {
  "xgboost-classifier": "xgboost.XGBClassifier",
  "xgboost-regressor": "xgboost.XGBRegressor",
  "xgboost-forecaster": "xgboost.XGBRegressor",
  "lightgbm-ranker": "lightgbm.LGBMRegressor",
  "lightgbm-regressor": "lightgbm.LGBMRegressor",
  "lightgbm-forecaster": "lightgbm.LGBMRegressor",
  "catboost-classifier": "catboost.CatBoostClassifier",
  "catboost-regressor": "catboost.CatBoostRegressor",
  "umap-reduction": "umap.UMAP",
};

// True time-series-only libraries — fit on a univariate series, not X/y
// tabular data. Maps to the real library timeseries_trainer.py fits.
export const TIME_SERIES_LIBRARY: Record<string, string> = {
  "prophet-forecaster": "prophet",
  "arima-forecaster": "arima",
  "sarima-forecaster": "sarima",
  "auto-arima-forecaster": "auto_arima",
  "exponential-smoothing": "exponential_smoothing",
  "holt-winters": "holt_winters",
};
export const TIME_SERIES_ONLY_IDS = new Set(Object.keys(TIME_SERIES_LIBRARY));

export function getClassicalClasspath(baseModelId: string, uri: string): string | null {
  if (uri.startsWith("sklearn://")) return uri.slice("sklearn://".length);
  return NON_SKLEARN_CLASSPATH[baseModelId] || null;
}

const TASK_MAP: Record<string, string> = {
  Classification: "classification",
  "Classification / regression": "classification",
  Regression: "regression",
  Clustering: "clustering",
  "Anomaly detection": "anomaly_detection",
  "Dimensionality reduction": "dimensionality_reduction",
  Ranking: "ranking",
  Forecasting: "forecasting",
};

export function mapToClassicalTask(task: string): string | null {
  return TASK_MAP[task] || null;
}
