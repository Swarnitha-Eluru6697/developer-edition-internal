import { describe, it, expect } from "vitest";
import { percentile } from "../src/routes/alerts";

describe("percentile (used for real p95-latency alert evaluation)", () => {
  it("computes a real p50 (median) correctly on an odd-length array", () => {
    expect(percentile([1, 2, 3, 4, 5], 50)).toBe(3);
  });

  it("computes p95 correctly — the actual alert-evaluation use case", () => {
    const values = Array.from({ length: 100 }, (_, i) => i + 1); // 1..100
    // p95 of 1..100 should land at or very near 95
    expect(percentile(values, 95)).toBeGreaterThanOrEqual(94);
    expect(percentile(values, 95)).toBeLessThanOrEqual(96);
  });

  it("handles a single-element array without throwing", () => {
    expect(percentile([42], 95)).toBe(42);
  });

  it("is not affected by input order — sorts internally", () => {
    expect(percentile([5, 1, 4, 2, 3], 50)).toBe(percentile([1, 2, 3, 4, 5], 50));
  });
});
