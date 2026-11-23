/*
 * FE-BUG-05: "the browser stopped watching" and "the backend operation
 * failed" are not the same thing, and must never be reported as if they
 * were. Polling a long-running action (an Operation for pipeline deploy/
 * pause/resume/restart, a DiscoveryRun for connection discover — see
 * pollOperation.ts/pollDiscoveryRun.ts, the two thin wrappers around this)
 * used to throw a "TimeoutError" the moment it gave up, which every caller
 * then treated exactly like a real failure (a red toast/dialog claiming the
 * thing itself failed) even though, on the backend, it may still be
 * `queued`/`running` and could well succeed a few seconds later.
 *
 * This module returns a discriminated PollOutcome instead of ever throwing
 * for "gave up watching" — the caller can render "still processing, here's
 * a button to keep watching" for `polling_timeout`/`network_disconnected`,
 * entirely separately from how it renders a real terminal `failed`/`stale`
 * result. Client polling state (`polling` while this loop runs, then one of
 * `polling_timeout`/`network_disconnected` if it gives up, vs. a resolved
 * PollOutcome once it stops) is deliberately never mixed into the server's
 * own operation/run status enum (queued/running/succeeded/failed/stale) —
 * two independent axes, not one conflated one.
 *
 * FE-BUG-06: the default (slowest) interval is OPERATION_POLL_MS (see
 * ../pollingPolicy.ts) — the "Operation in progress" tier of the polling
 * cadence matrix, not an arbitrary number picked independently of it.
 * The first checks are quicker (150ms, 300ms, 600ms, 1.2s, then that interval): a discover
 * that the backend finishes in 40ms used to show up only after a full 3s wait.
 */
import { OPERATION_POLL_MS } from "../pollingPolicy";

export type PollOutcome<T> =
  | { kind: "terminal"; value: T }
  | { kind: "polling_timeout"; lastValue: T; timeoutMs: number }
  | { kind: "network_disconnected"; lastValue: T | null; error: unknown };

export interface PollUntilTerminalOptions {
  /** The longest delay between polls. The first checks come much sooner (FIRST_POLL_MS, doubling) because most actions
   * — a discover, a pause, a restart — finish within a fraction of a second; only a long-running one settles at this pace. */
  intervalMs?: number;
  /** Stop waiting (not: report a failure) once this much time has passed
   * without reaching a terminal status. */
  timeoutMs?: number;
}

/**
 * Repeatedly calls `fetchOne()` until `isTerminal(value)` holds, `timeoutMs`
 * elapses, or `fetchOne()` itself throws (a transient failure already
 * exhausted its own retries — see api.ts's withRetry — by the time that
 * reaches here, so this doesn't retry it again; it reports
 * `network_disconnected` and lets the caller decide whether to keep
 * watching). Never throws itself: giving up, for any reason, is a normal
 * outcome this function reports, not an exceptional one.
 */
/** The wait before the second check. Doubles after each check until it reaches the caller's intervalMs. */
export const FIRST_POLL_MS = 150;

export async function pollUntilTerminal<T>(
  fetchOne: () => Promise<T>,
  isTerminal: (value: T) => boolean,
  { intervalMs = OPERATION_POLL_MS, timeoutMs = 30000 }: PollUntilTerminalOptions = {},
): Promise<PollOutcome<T>> {
  const deadline = Date.now() + timeoutMs;
  let lastValue: T | null = null;
  let delay = Math.min(FIRST_POLL_MS, intervalMs);
  for (;;) {
    let value: T;
    try {
      value = await fetchOne();
    } catch (error) {
      return { kind: "network_disconnected", lastValue, error };
    }
    lastValue = value;
    if (isTerminal(value)) return { kind: "terminal", value };
    if (Date.now() > deadline) return { kind: "polling_timeout", lastValue: value, timeoutMs };
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, intervalMs);
  }
}
