import { afterEach, describe, expect, it, vi } from "vitest";
import { MAX_TIMER_MS, setLongTimeout } from "../src/services/longTimeout";

const MONTH_MS = 30 * 24 * 60 * 60 * 1000;   // 2,592,000,000 — past Node's timer cap

describe("setLongTimeout", () => {
  afterEach(() => vi.useRealTimers());

  it("a one-month delay does not fire early (a plain setTimeout would fire after 1 ms)", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    setLongTimeout(fn, MONTH_MS);
    vi.advanceTimersByTime(MAX_TIMER_MS);           // past the cap
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(MONTH_MS - MAX_TIMER_MS - 1);
    expect(fn).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("clear() cancels, including after the first step", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const t = setLongTimeout(fn, MONTH_MS);
    vi.advanceTimersByTime(MAX_TIMER_MS + 10);
    t.clear();
    vi.advanceTimersByTime(MONTH_MS);
    expect(fn).not.toHaveBeenCalled();
  });
});
