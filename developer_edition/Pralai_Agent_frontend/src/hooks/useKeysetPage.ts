import { useCallback, useEffect, useState } from 'react';
import type { Page } from '../api';

/** Keyset pagination for one list.
 *
 * Extracted because the Alerts page needs it three times — alerts,
 * rules and channels — and the fiddly parts are the ones that go wrong
 * when copied:
 *
 *   * a cursor points into ONE ordered result set, so any change to
 *     what is being listed invalidates every cursor already held;
 *   * the offset has to be carried forward from the rows actually
 *     returned, not derived from pageIndex * pageSize, because one
 *     short page makes every later row number wrong and it never
 *     recovers.
 */
export interface KeysetPage<T> {
  items: T[];
  loading: boolean;
  hasMore: boolean;
  /** 0-based. */
  pageIndex: number;
  /** How many rows came before the first one on screen. */
  offset: number;
  pageSize: number;
  setPageSize: (n: number) => void;
  next: () => void;
  previous: () => void;
  /** Re-fetch the current page, e.g. after a delete. */
  reload: () => void;
  /** Back to page one — for when what is being listed changes. */
  reset: () => void;
}

export function useKeysetPage<T>(
  fetchPage: (cursor: string | null, limit: number) => Promise<Page<T>>,
  // Anything that changes WHAT is listed rather than which page of it.
  deps: unknown[],
  // False while the list is not on screen. Without this every list on a
  // tabbed page fetches on mount, so opening Alerts made three requests
  // for two tabs nobody was looking at.
  enabled = true,
  initialPageSize = 25,
): KeysetPage<T> {
  const [items, setItems] = useState<T[]>([]);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [pageSize, setPageSize] = useState(initialPageSize);
  const [pages, setPages] = useState<{ cursor: string | null; offset: number }[]>(
    [{ cursor: null, offset: 0 }]);
  const [tick, setTick] = useState(0);

  const pageIndex = pages.length - 1;
  const offset = pages[pageIndex].offset;

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setLoading(true);
    fetchPage(pages[pageIndex].cursor, pageSize)
      .then((page) => {
        // A response that arrived after the inputs changed describes a
        // list nobody is looking at any more.
        if (cancelled) return;
        setItems(page.items);
        setHasMore(page.hasMore);
        setNextCursor(page.nextCursor);
      })
      .catch(() => { if (!cancelled) { setItems([]); setHasMore(false); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, pages, pageIndex, pageSize, tick, ...deps]);

  // Changing what is listed, or how many rows a page holds, makes every
  // held cursor point into a different result set.
  useEffect(() => {
    setPages([{ cursor: null, offset: 0 }]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageSize, ...deps]);

  const next = useCallback(() => {
    setPages((p) => [...p, { cursor: nextCursor, offset: offset + items.length }]);
  }, [nextCursor, offset, items.length]);

  const previous = useCallback(() => setPages((p) => p.slice(0, -1)), []);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const reset = useCallback(() => setPages([{ cursor: null, offset: 0 }]), []);

  return { items, loading, hasMore, pageIndex, offset, pageSize,
           setPageSize, next, previous, reload, reset };
}
