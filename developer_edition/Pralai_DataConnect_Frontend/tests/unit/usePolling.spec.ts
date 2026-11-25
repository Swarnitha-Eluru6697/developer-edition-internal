/*
 * Polling backoff/jitter arithmetic — the pure part of src/hooks/usePolling.ts
 * (computeInterval), tested head-on without needing a DOM. The React-glue
 * half (the visibilitychange listener, immediate refetch on refocus) is
 * covered by tests/e2e/polling.spec.ts instead, since it's only meaningful
 * against a real page/tab.
 */
import { test, expect } from "@playwright/test";
import { computeInterval } from "../../src/hooks/usePolling";

const noJitter = () => 0.5; // random() = 0.5 → jitter multiplier of exactly 1 (no adjustment)

test.describe("computeInterval", () => {
  test("is 0 when disabled, regardless of visibility", () => {
    expect(computeInterval(false, true, 5000, 60_000, 0, noJitter)).toBe(0);
  });

  test("is 0 when the tab is hidden, regardless of enabled", () => {
    expect(computeInterval(true, false, 5000, 60_000, 0, noJitter)).toBe(0);
  });

  test("is baseMs when enabled, visible, and healthy (no jitter)", () => {
    expect(computeInterval(true, true, 5000, 60_000, 0, noJitter)).toBe(5000);
  });

  test("doubles per consecutive failure", () => {
    expect(computeInterval(true, true, 1000, 60_000, 1, noJitter)).toBe(2000);
    expect(computeInterval(true, true, 1000, 60_000, 2, noJitter)).toBe(4000);
    expect(computeInterval(true, true, 1000, 60_000, 3, noJitter)).toBe(8000);
  });

  test("never exceeds maxMs regardless of how many failures", () => {
    expect(computeInterval(true, true, 1000, 10_000, 10, noJitter)).toBe(10_000);
    expect(computeInterval(true, true, 1000, 10_000, 100, noJitter)).toBe(10_000);
  });

  test("never drops below a 1s floor even for a tiny baseMs", () => {
    expect(computeInterval(true, true, 100, 60_000, 0, noJitter)).toBe(1000);
  });

  test("applies up to ±20% jitter around the backed-off value", () => {
    const atMin = computeInterval(true, true, 5000, 60_000, 0, () => 0); // jitter multiplier 0.8
    const atMax = computeInterval(true, true, 5000, 60_000, 0, () => 1); // jitter multiplier 1.2
    expect(atMin).toBe(4000);
    expect(atMax).toBe(6000);
  });

  test("resetting consecutive failures to 0 returns to the base interval", () => {
    // Simulates reportResult(null) after a recovered poll: failures reset,
    // the very next computeInterval call is back to steady-state.
    expect(computeInterval(true, true, 2000, 60_000, 0, noJitter)).toBe(2000);
  });
});
