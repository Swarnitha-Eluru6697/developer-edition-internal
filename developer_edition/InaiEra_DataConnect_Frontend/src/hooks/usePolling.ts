/*
 * Centralized polling policy for RTK Query.
 *
 * Before this existed, a page that wanted auto-refresh (Pipelines.tsx) just
 * hardcoded `pollingInterval: 5000` inline: no pause when the tab is in the
 * background, no backoff when the backend is failing (every tick keeps
 * hitting it at the same fixed rate), no jitter, and the pattern would have
 * been copy-pasted, not shared, the next time another page wanted the same
 * thing. This hook is the one place that policy lives now.
 *
 * It deliberately only ever *computes a number* — the `pollingInterval` RTK
 * Query's own `useXQuery(arg, { pollingInterval })` already accepts and
 * dynamically re-reads on every render. That's what keeps "shared cache" and
 * "no duplicate polling across components" true for free: two components
 * calling `useGetPipelinesQuery(undefined, { pollingInterval })` — whatever
 * interval each currently passes — still subscribe to the exact same RTK
 * Query cache entry and the exact same underlying poll loop (RTK Query polls
 * at the fastest interval any current subscriber requests); nothing here
 * reimplements that, it would only be able to make it worse.
 */
import { useCallback, useEffect, useRef, useState } from "react";

function isTabVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

/** Fires `onVisible` every time the tab transitions from hidden to visible — e.g. to refetch immediately instead of waiting out the rest of a paused poll interval. */
export function useOnVisible(onVisible: () => void): void {
  const ref = useRef(onVisible);
  ref.current = onVisible;
  useEffect(() => {
    if (typeof document === "undefined") return;
    const handler = () => { if (document.visibilityState === "visible") ref.current(); };
    document.addEventListener("visibilitychange", handler);
    return () => document.removeEventListener("visibilitychange", handler);
  }, []);
}

export interface UsePollingOptions {
  /** The caller's own on/off switch (a UI toggle, "no drawer open", ...) — independent of tab visibility. */
  enabled?: boolean;
  /** Steady-state interval once healthy and visible. */
  baseMs?: number;
  /** Ceiling for exponential backoff after consecutive failures. */
  maxMs?: number;
}

export type ReportPollResult = (error: unknown) => void;

/**
 * The pure backoff/jitter arithmetic, pulled out of the hook so it's
 * testable head-on (tests/unit/usePolling.spec.ts) without needing a DOM —
 * `usePolling` itself is only React glue (state, the visibilitychange
 * listener) around this.
 */
export function computeInterval(
  enabled: boolean,
  visible: boolean,
  baseMs: number,
  maxMs: number,
  consecutiveFailures: number,
  random: () => number = Math.random,
): number {
  if (!enabled || !visible) return 0;
  const backedOff = Math.min(maxMs, baseMs * 2 ** consecutiveFailures);
  const jitter = 1 + (random() * 0.4 - 0.2); // ±20%
  return Math.max(1000, Math.round(backedOff * jitter));
}

/**
 * Returns `[pollingInterval, reportResult]`.
 *
 * `pollingInterval` is what to pass straight to an RTK Query hook. It is `0`
 * (i.e. polling paused) whenever `enabled` is false or the tab is hidden —
 * `document.visibilitychange` is the actual signal, not a guess based on
 * focus/blur, which fires for plenty of non-backgrounding reasons (opening
 * devtools, a native `<select>` dropdown). Otherwise it's `baseMs`, doubled
 * per consecutive reported failure up to `maxMs`, with up to ±20% jitter so
 * many clients polling the same endpoint don't all land on the backend in
 * the same instant.
 *
 * `reportResult` must be called with the polled query's own `error` after
 * every completed attempt (via a `useEffect` watching it — see
 * Pipelines.tsx) — that's what drives the backoff, and what resets it back
 * to `baseMs` the moment a poll succeeds again.
 */
export function usePolling({ enabled = true, baseMs = 5000, maxMs = 60_000 }: UsePollingOptions): [number, ReportPollResult] {
  const [visible, setVisible] = useState(isTabVisible);
  useEffect(() => {
    if (typeof document === "undefined") return;
    const handler = () => setVisible(isTabVisible());
    document.addEventListener("visibilitychange", handler);
    return () => document.removeEventListener("visibilitychange", handler);
  }, []);

  const consecutiveFailuresRef = useRef(0);
  const [interval, setInterval_] = useState(0);

  const recompute = useCallback(() => {
    setInterval_(computeInterval(enabled, visible, baseMs, maxMs, consecutiveFailuresRef.current));
  }, [enabled, visible, baseMs, maxMs]);

  useEffect(recompute, [recompute]);

  const reportResult = useCallback<ReportPollResult>((error) => {
    consecutiveFailuresRef.current = error != null ? consecutiveFailuresRef.current + 1 : 0;
    recompute();
  }, [recompute]);

  return [interval, reportResult];
}
