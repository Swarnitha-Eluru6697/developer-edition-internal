import { describe, it, expect } from "vitest";
import { checkDatasetQualityGate } from "../src/services/modelRegistration";

const baseProfile = (overrides: Partial<any> = {}) => ({
  ok: true,
  rowCount: 100,
  duplicateRowCount: 0,
  columns: [
    { name: "amount", kind: "numeric", missingPct: 0, std: 12.5 },
    { name: "is_fraudulent", kind: "numeric", missingPct: 0, std: 0.3 },
  ],
  ...overrides,
});

describe("checkDatasetQualityGate", () => {
  it("passes clean data with no blockers or warnings", () => {
    const result = checkDatasetQualityGate(baseProfile(), "is_fraudulent");
    expect(result.blocked).toBe(false);
    expect(result.blockers).toHaveLength(0);
  });

  it("blocks when the target column is too sparse (>20% missing)", () => {
    const profile = baseProfile({
      columns: [
        { name: "amount", kind: "numeric", missingPct: 0, std: 12.5 },
        { name: "is_fraudulent", kind: "numeric", missingPct: 40, std: 0.3 },
      ],
    });
    const result = checkDatasetQualityGate(profile, "is_fraudulent");
    expect(result.blocked).toBe(true);
    expect(result.blockers[0]).toContain("40% missing");
  });

  it("blocks when the target column has zero variance — nothing to learn from", () => {
    const profile = baseProfile({
      columns: [
        { name: "amount", kind: "numeric", missingPct: 0, std: 12.5 },
        { name: "is_fraudulent", kind: "numeric", missingPct: 0, std: 0 },
      ],
    });
    const result = checkDatasetQualityGate(profile, "is_fraudulent");
    expect(result.blocked).toBe(true);
    expect(result.blockers[0]).toContain("zero variance");
  });

  it("blocks a categorical target with only one real distinct value", () => {
    const profile = baseProfile({
      columns: [{ name: "label", kind: "categorical", missingPct: 0, cardinality: 1 }],
    });
    const result = checkDatasetQualityGate(profile, "label");
    expect(result.blocked).toBe(true);
    expect(result.blockers[0]).toContain("only one real distinct value");
  });

  it("warns, but does not block, for a sparse FEATURE column (not the target)", () => {
    const profile = baseProfile({
      columns: [
        { name: "amount", kind: "numeric", missingPct: 60, std: 12.5 },
        { name: "is_fraudulent", kind: "numeric", missingPct: 0, std: 0.3 },
      ],
    });
    const result = checkDatasetQualityGate(profile, "is_fraudulent");
    expect(result.blocked).toBe(false);
    expect(result.warnings[0]).toContain("amount");
  });

  it("does not gate on a missing/failed profile — nothing real to check against", () => {
    const result = checkDatasetQualityGate({ ok: false }, "is_fraudulent");
    expect(result.blocked).toBe(false);
    expect(result.warnings).toHaveLength(0);
  });

  it("blocks a genuinely empty dataset", () => {
    const result = checkDatasetQualityGate(baseProfile({ rowCount: 0, columns: [] }), null);
    expect(result.blocked).toBe(true);
    expect(result.blockers[0]).toContain("0 rows");
  });

  it("blocks a dataset too small for a real train/test split", () => {
    const result = checkDatasetQualityGate(baseProfile({ rowCount: 5 }), "is_fraudulent");
    expect(result.blocked).toBe(true);
    expect(result.blockers[0]).toContain("not enough for a meaningful train/test split");
  });

  it("warns (does not block) for a small but split-feasible dataset", () => {
    const result = checkDatasetQualityGate(baseProfile({ rowCount: 30 }), "is_fraudulent");
    expect(result.blocked).toBe(false);
    expect(result.warnings.some((w) => w.includes("only 30 real rows"))).toBe(true);
  });

  it("blocks when a real target class has fewer than 2 samples — can't split", () => {
    const profile = baseProfile({
      columns: [
        { name: "label", kind: "categorical", missingPct: 0, cardinality: 2, allValueCounts: [{ value: "no", count: 99 }, { value: "yes", count: 1 }] },
      ],
    });
    const result = checkDatasetQualityGate(profile, "label");
    expect(result.blocked).toBe(true);
    expect(result.blockers[0]).toContain("only 1 real sample");
  });

  it("warns (does not block) for a rare but splittable target class", () => {
    const profile = baseProfile({
      columns: [
        { name: "label", kind: "categorical", missingPct: 0, cardinality: 2, allValueCounts: [{ value: "no", count: 97 }, { value: "yes", count: 3 }] },
      ],
    });
    const result = checkDatasetQualityGate(profile, "label");
    expect(result.blocked).toBe(false);
    expect(result.warnings.some((w) => w.includes("real class imbalance"))).toBe(true);
  });

  it("warns on a constant feature column — carries no real signal", () => {
    const profile = baseProfile({
      columns: [
        { name: "amount", kind: "numeric", missingPct: 0, std: 12.5 },
        { name: "always_us", kind: "categorical", missingPct: 0, cardinality: 1, isConstant: true },
        { name: "is_fraudulent", kind: "numeric", missingPct: 0, std: 0.3 },
      ],
    });
    const result = checkDatasetQualityGate(profile, "is_fraudulent");
    expect(result.blocked).toBe(false);
    expect(result.warnings.some((w) => w.includes("always_us") && w.includes("constant"))).toBe(true);
  });

  it("warns on a feature with extreme cardinality — likely an identifier", () => {
    const profile = baseProfile({
      rowCount: 1000,
      columns: [
        { name: "user_id", kind: "categorical", missingPct: 0, cardinality: 998, cardinalityRatio: 0.998 },
        { name: "is_fraudulent", kind: "numeric", missingPct: 0, std: 0.3 },
      ],
    });
    const result = checkDatasetQualityGate(profile, "is_fraudulent");
    expect(result.blocked).toBe(false);
    expect(result.warnings.some((w) => w.includes("user_id") && w.includes("extreme cardinality"))).toBe(true);
  });

  it("warns on a feature with a high real outlier rate", () => {
    const profile = baseProfile({
      columns: [
        { name: "amount", kind: "numeric", missingPct: 0, std: 12.5, outlierPct: 15 },
        { name: "is_fraudulent", kind: "numeric", missingPct: 0, std: 0.3 },
      ],
    });
    const result = checkDatasetQualityGate(profile, "is_fraudulent");
    expect(result.blocked).toBe(false);
    expect(result.warnings.some((w) => w.includes("amount") && w.includes("outliers"))).toBe(true);
  });

  it("blocks on near-perfect leakage — the feature IS the target", () => {
    const profile = baseProfile({
      columns: [
        { name: "leaky", kind: "numeric", missingPct: 0, std: 0.3, leakageSignal: { kind: "correlation", value: 0.999 } },
        { name: "is_fraudulent", kind: "numeric", missingPct: 0, std: 0.3 },
      ],
    });
    const result = checkDatasetQualityGate(profile, "is_fraudulent");
    expect(result.blocked).toBe(true);
    expect(result.blockers[0]).toContain("leaky");
    expect(result.blockers[0]).toContain("leakage");
  });

  it("warns (does not block) on strong but imperfect leakage", () => {
    const profile = baseProfile({
      columns: [
        { name: "suspicious", kind: "numeric", missingPct: 0, std: 0.3, leakageSignal: { kind: "correlation", value: 0.93 } },
        { name: "is_fraudulent", kind: "numeric", missingPct: 0, std: 0.3 },
      ],
    });
    const result = checkDatasetQualityGate(profile, "is_fraudulent");
    expect(result.blocked).toBe(false);
    expect(result.warnings.some((w) => w.includes("suspicious"))).toBe(true);
  });

  it("does not flag a genuinely weak, real correlation as leakage", () => {
    const profile = baseProfile({
      columns: [
        { name: "amount", kind: "numeric", missingPct: 0, std: 12.5, leakageSignal: { kind: "correlation", value: 0.12 } },
        { name: "is_fraudulent", kind: "numeric", missingPct: 0, std: 0.3 },
      ],
    });
    const result = checkDatasetQualityGate(profile, "is_fraudulent");
    expect(result.blocked).toBe(false);
    expect(result.warnings.some((w) => w.includes("amount"))).toBe(false);
  });
});
