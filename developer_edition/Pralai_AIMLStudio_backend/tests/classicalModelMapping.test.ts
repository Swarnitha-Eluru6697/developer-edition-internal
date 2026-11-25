import { describe, it, expect } from "vitest";
import { getClassicalClasspath, mapToClassicalTask, TIME_SERIES_LIBRARY } from "../src/services/classicalModelMapping";

describe("getClassicalClasspath", () => {
  it("derives a real Python classpath directly from a sklearn:// uri", () => {
    expect(getClassicalClasspath("random-forest", "sklearn://sklearn.ensemble.RandomForestClassifier")).toBe(
      "sklearn.ensemble.RandomForestClassifier"
    );
  });

  it("resolves the 9 non-sklearn base models via their known real library classes", () => {
    expect(getClassicalClasspath("xgboost-classifier", "oci://registry.inaiera.io/mlops/trainer-xgboost:1.4")).toBe(
      "xgboost.XGBClassifier"
    );
    expect(getClassicalClasspath("umap-reduction", "oci://registry.inaiera.io/mlops/trainer-umap:1.0")).toBe("umap.UMAP");
  });

  it("returns null for a base model with neither a sklearn:// uri nor a known mapping (e.g. a true time-series model)", () => {
    expect(getClassicalClasspath("arima-forecaster", "oci://registry.inaiera.io/mlops/trainer-arima:1.0")).toBeNull();
  });
});

describe("mapToClassicalTask", () => {
  it("maps every real catalog task string this scaffold uses", () => {
    expect(mapToClassicalTask("Classification")).toBe("classification");
    expect(mapToClassicalTask("Classification / regression")).toBe("classification");
    expect(mapToClassicalTask("Regression")).toBe("regression");
    expect(mapToClassicalTask("Clustering")).toBe("clustering");
    expect(mapToClassicalTask("Anomaly detection")).toBe("anomaly_detection");
    expect(mapToClassicalTask("Dimensionality reduction")).toBe("dimensionality_reduction");
    expect(mapToClassicalTask("Ranking")).toBe("ranking");
    expect(mapToClassicalTask("Forecasting")).toBe("forecasting");
  });

  it("returns null for an unrecognized task string rather than guessing", () => {
    expect(mapToClassicalTask("Something new")).toBeNull();
  });
});

describe("TIME_SERIES_LIBRARY", () => {
  it("covers exactly the 6 true time-series base models", () => {
    expect(Object.keys(TIME_SERIES_LIBRARY).sort()).toEqual(
      ["arima-forecaster", "auto-arima-forecaster", "exponential-smoothing", "holt-winters", "prophet-forecaster", "sarima-forecaster"].sort()
    );
  });
});
