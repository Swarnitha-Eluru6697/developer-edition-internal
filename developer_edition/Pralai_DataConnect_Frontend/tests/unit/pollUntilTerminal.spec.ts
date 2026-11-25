/*
 * FE-BUG-05: pollUntilTerminal must report "gave up watching" as its own
 * distinct outcome — never a thrown error, and never conflated with a real
 * terminal result — see src/store/pollUntilTerminal.ts's own header comment.
 * Tested head-on with tiny real timeouts (a few ms) rather than mocked
 * timers: the function only ever calls Date.now()/setTimeout, so real short
 * delays are both simpler and faster than faking them.
 */
import { test, expect } from "@playwright/test";
import { pollUntilTerminal } from "../../src/store/pollUntilTerminal";

function isTerminalStatus(v: { status: string }): boolean {
  return v.status === "succeeded" || v.status === "failed";
}

test.describe("pollUntilTerminal", () => {
  test("resolves with a terminal outcome the moment isTerminal holds, without waiting out the timeout", async () => {
    let calls = 0;
    const fetchOne = async () => {
      calls += 1;
      return { status: calls < 3 ? "running" : "succeeded" };
    };
    const outcome = await pollUntilTerminal(fetchOne, isTerminalStatus, { intervalMs: 5, timeoutMs: 10_000 });
    expect(outcome).toEqual({ kind: "terminal", value: { status: "succeeded" } });
    expect(calls).toBe(3);
  });

  test("reports polling_timeout — not an error — when the deadline passes without a terminal status", async () => {
    const fetchOne = async () => ({ status: "running" });
    const outcome = await pollUntilTerminal(fetchOne, isTerminalStatus, { intervalMs: 5, timeoutMs: 30 });
    expect(outcome.kind).toBe("polling_timeout");
    if (outcome.kind === "polling_timeout") {
      // The last actually-observed server state travels with the outcome —
      // still "running", never silently reported as failed.
      expect(outcome.lastValue).toEqual({ status: "running" });
      expect(outcome.timeoutMs).toBe(30);
    }
  });

  test("reports network_disconnected — not an error — when fetchOne itself throws", async () => {
    const fetchOne = async () => {
      throw new Error("Network error — is the backend reachable?");
    };
    const outcome = await pollUntilTerminal(fetchOne, isTerminalStatus, { intervalMs: 5, timeoutMs: 10_000 });
    expect(outcome.kind).toBe("network_disconnected");
    if (outcome.kind === "network_disconnected") {
      expect(outcome.lastValue).toBeNull();
      expect((outcome.error as Error).message).toContain("Network error");
    }
  });

  test("network_disconnected still carries the last value seen before the failure", async () => {
    let calls = 0;
    const fetchOne = async () => {
      calls += 1;
      if (calls === 1) return { status: "running" };
      throw new Error("connection reset");
    };
    const outcome = await pollUntilTerminal(fetchOne, isTerminalStatus, { intervalMs: 5, timeoutMs: 10_000 });
    expect(outcome.kind).toBe("network_disconnected");
    if (outcome.kind === "network_disconnected") {
      expect(outcome.lastValue).toEqual({ status: "running" });
    }
  });

  test("never throws for either give-up reason — both are ordinary return values", async () => {
    // If pollUntilTerminal rejected instead of resolving, these awaits
    // would themselves fail the test — no try/catch needed to prove it.
    const timedOut = await pollUntilTerminal(
      async () => ({ status: "running" }), isTerminalStatus, { intervalMs: 5, timeoutMs: 10 },
    );
    expect(timedOut.kind).toBe("polling_timeout");

    const disconnected = await pollUntilTerminal(
      async () => { throw new Error("boom"); }, isTerminalStatus, { intervalMs: 5, timeoutMs: 10_000 },
    );
    expect(disconnected.kind).toBe("network_disconnected");
  });

  // A discover the backend finishes in ~40ms used to be shown only after a flat 3s wait. The first checks are now quick and back off.
  test("an action that finishes straight away is seen within a fraction of a second, even with a slow cap", async () => {
    let calls = 0;
    const started = Date.now();
    const outcome = await pollUntilTerminal(async () => ({ status: ++calls < 2 ? "running" : "succeeded" }), isTerminalStatus, { intervalMs: 3000, timeoutMs: 10_000 });
    expect(outcome.kind).toBe("terminal");
    expect(Date.now() - started).toBeLessThan(800);                         // one short wait (150ms), not the 3000ms cap
  });

  test("the waits grow (150, 300, 600, ...) up to the cap, so a long job is not polled hard", async () => {
    const stamps: number[] = [];
    await pollUntilTerminal(async () => { stamps.push(Date.now()); return { status: stamps.length < 5 ? "running" : "succeeded" }; }, isTerminalStatus, { intervalMs: 400, timeoutMs: 10_000 });
    const gaps = stamps.slice(1).map((t, i) => t - stamps[i]!);
    expect(gaps.length).toBe(4);
    expect(gaps[0]!).toBeLessThan(260);                                     // ~150
    expect(gaps[1]!).toBeGreaterThan(gaps[0]!);                             // ~300: longer
    expect(gaps[2]!).toBeGreaterThan(330);                                  // ~400: reached the cap
    expect(gaps[3]!).toBeLessThan(520);                                     // and stays there
  });
});
