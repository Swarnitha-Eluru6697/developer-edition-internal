/*
 * A choice that survives a page refresh: which tab is open, which range is picked. Stored in localStorage under its own key — the same convention
 * the app uses for its last page (DataConnectApp.tsx's dc_last_page) — read once when the page mounts and written on
 * every change. Only a value in `allowed` is accepted back, so a stale or edited entry falls back to `fallback`; storage being unavailable
 * (private mode, blocked) is not an error, the choice just lasts until the refresh.
 */
import { useCallback, useState } from "react";

export function usePersistedChoice<T extends string>(key: string, allowed: readonly T[], fallback: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = typeof localStorage !== "undefined" ? localStorage.getItem(key) : null;
      return allowed.includes(stored as T) ? (stored as T) : fallback;
    } catch {
      return fallback;
    }
  });
  const set = useCallback((next: T) => {
    setValue(next);
    try { if (typeof localStorage !== "undefined") localStorage.setItem(key, next); } catch { /* the choice lasts until the refresh */ }
  }, [key]);
  return [value, set];
}
