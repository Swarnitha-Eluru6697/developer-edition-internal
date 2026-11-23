/** Node's timers cap at 2^31-1 ms (~24.8 days). A larger delay isn't honoured —
 *  Node warns and fires it after 1 ms — so a month-long training ceiling would have
 *  killed every run instantly. This chains timers in capped steps instead. */
export const MAX_TIMER_MS = 2_147_483_647;

export interface LongTimeout { clear(): void }

export function setLongTimeout(fn: () => void, ms: number): LongTimeout {
  let handle: NodeJS.Timeout;
  let remaining = Number.isFinite(ms) && ms > 0 ? ms : 0;
  const step = () => {
    if (remaining <= MAX_TIMER_MS) { handle = setTimeout(fn, remaining); return; }
    remaining -= MAX_TIMER_MS;
    handle = setTimeout(step, MAX_TIMER_MS);
  };
  step();
  return { clear: () => clearTimeout(handle) };
}
