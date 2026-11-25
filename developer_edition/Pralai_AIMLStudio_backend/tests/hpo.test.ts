import { describe, it, expect } from "vitest";
import { sampleHyperparams, isDeepLearningFramework, hashStringToInt } from "../src/routes/hpo";

describe("sampleHyperparams (mixed-radix trial sampler)", () => {
  const searchSpace = { hiddenLayers: [[32], [64], [128]], learningRate: [0.001, 0.01] };

  it("enumerates every combination exactly once across the full space (3 * 2 = 6 trials)", () => {
    const seen = new Set<string>();
    for (let i = 0; i < 6; i++) {
      seen.add(JSON.stringify(sampleHyperparams(searchSpace, i)));
    }
    expect(seen.size).toBe(6);
  });

  it("wraps deterministically past the full space size — same trial index always gives the same combo", () => {
    const a = sampleHyperparams(searchSpace, 4);
    const b = sampleHyperparams(searchSpace, 4);
    expect(a).toEqual(b);
    const wrapped = sampleHyperparams(searchSpace, 4 + 6); // one full cycle later
    expect(wrapped).toEqual(a);
  });

  it("varies the first parameter across its own natural cycle, and correctly repeats it at exact multiples of its own radix — real mixed-radix behavior, not a stride bug", () => {
    // hiddenLayers has 3 options and is the first ("least significant")
    // key, so it cycles every 3 trials by construction — same as how a
    // base-10 counter's ones-digit repeats every 10, not a bug. The
    // property actually worth verifying: it DOES vary across its own
    // cycle (0,1,2 differ), and genuinely repeats at multiples of 3.
    const cycle = [0, 1, 2].map((i) => JSON.stringify(sampleHyperparams(searchSpace, i).hiddenLayers));
    expect(new Set(cycle).size).toBe(3); // all 3 options seen within one cycle
    expect(sampleHyperparams(searchSpace, 0).hiddenLayers).toEqual(sampleHyperparams(searchSpace, 3).hiddenLayers);
  });

  it("handles a single-parameter search space", () => {
    const space = { n_estimators: [50, 100, 150] };
    expect(sampleHyperparams(space, 0).n_estimators).toBe(50);
    expect(sampleHyperparams(space, 1).n_estimators).toBe(100);
    expect(sampleHyperparams(space, 2).n_estimators).toBe(150);
    expect(sampleHyperparams(space, 3).n_estimators).toBe(50); // wraps
  });
});

describe("isDeepLearningFramework", () => {
  it("recognizes real deep-learning frameworks, case-insensitively", () => {
    expect(isDeepLearningFramework("pytorch")).toBe(true);
    expect(isDeepLearningFramework("PyTorch")).toBe(true);
    expect(isDeepLearningFramework("tensorflow")).toBe(true);
    expect(isDeepLearningFramework("keras")).toBe(true);
    expect(isDeepLearningFramework("jax")).toBe(true);
  });

  it("does not misclassify classical-ML frameworks as deep learning", () => {
    expect(isDeepLearningFramework("scikit-learn")).toBe(false);
    expect(isDeepLearningFramework("xgboost")).toBe(false);
    expect(isDeepLearningFramework("lightgbm")).toBe(false);
    expect(isDeepLearningFramework("statsmodels")).toBe(false);
  });
});

describe("hashStringToInt", () => {
  it("is deterministic — same input always produces the same output", () => {
    expect(hashStringToInt("run-abc-123")).toBe(hashStringToInt("run-abc-123"));
  });

  it("produces different values for different inputs (not a constant)", () => {
    expect(hashStringToInt("run-a")).not.toBe(hashStringToInt("run-b"));
  });
});
