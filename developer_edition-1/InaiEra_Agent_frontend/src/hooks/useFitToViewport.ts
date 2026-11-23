import { useEffect, useRef } from 'react';

/** Cap a scrolling element's height at the space left below it on screen.
 *
 *  A .table-scroll frame keeps its column headers fixed by scrolling the rows
 *  inside itself — but only while the frame fits on screen. A fixed
 *  `calc(100vh - 340px)` guessed at what sits above it; on a page with a
 *  taller header (Traces' subtitle wraps to two lines) the frame was taller
 *  than the room left, so the PAGE scrolled first and took the headers with
 *  it. Measured instead: the frame's own top within the scrolling .main, and
 *  whatever follows it in its card (the pager), re-measured on resize. */
export function useFitToViewport<T extends HTMLElement>(deps: unknown[] = []) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const el = ref.current;
    const main = el?.closest('.main') as HTMLElement | null;
    if (!el || !main) return;
    const fit = () => {
      // Position as laid out at scroll 0, so the answer doesn't depend on
      // how far the page happens to be scrolled when this runs.
      const top = el.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop;
      let below = 0;
      for (let s = el.nextElementSibling; s; s = s.nextElementSibling) below += (s as HTMLElement).offsetHeight;
      const wrapPad = parseFloat(getComputedStyle(el.closest('.wrap') ?? main).paddingBottom) || 0;
      const room = main.clientHeight - top - below - wrapPad - 2; // 2: the card's borders
      el.style.maxHeight = `${Math.max(200, Math.floor(room))}px`;
    };
    fit();
    window.addEventListener('resize', fit);
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(fit) : null;
    const head = main.querySelector('.pagehead');
    if (ro && head) ro.observe(head);
    return () => { window.removeEventListener('resize', fit); ro?.disconnect(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}
