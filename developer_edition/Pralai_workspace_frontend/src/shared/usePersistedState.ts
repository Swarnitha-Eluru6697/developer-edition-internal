/**
 * State that survives a page refresh.
 *
 * The sidebar section already did (uiSlice `inaiera.navSection`), but a screen's own place — the job you opened, its tab, a selected
 * binding — lived only in component state, so a refresh dropped you back at the list. This is `useState` that also keeps its value in
 * sessionStorage (per browser tab, like the section: a new tab starts clean, a refresh keeps its place).
 *
 * What is stored is validated on the way in (`isValid`): a value from an older build, or a hand-edited one, falls back to `initial`
 * instead of breaking the screen. Storage that is unavailable (private mode, quota) just means nothing is remembered.
 */
import { useCallback, useEffect, useRef, useState } from "react";

const PREFIX = "inaiera.ui.";

export function readPersisted<T>(key: string, initial: T, isValid: (v: unknown) => v is T): T {
  try {
    const raw = window.sessionStorage.getItem(PREFIX + key);
    if (raw === null) return initial;
    const value: unknown = JSON.parse(raw);
    return isValid(value) ? value : initial;
  } catch {
    return initial;
  }
}

export function writePersisted<T>(key: string, value: T, initial: T): void {
  try {
    // The default is stored as "nothing": a screen that is back at its start leaves no trace behind.
    if (JSON.stringify(value) === JSON.stringify(initial)) window.sessionStorage.removeItem(PREFIX + key);
    else window.sessionStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch { /* storage unavailable: the place just is not remembered */ }
}

/** Forget every remembered place whose key starts with `prefix` (e.g. "jobs.sql."). */
export function forgetPersisted(prefix: string): void {
  try {
    const wanted = PREFIX + prefix;
    const keys: string[] = [];
    for (let i = 0; i < window.sessionStorage.length; i++) {
      const key = window.sessionStorage.key(i);
      if (key && key.startsWith(wanted)) keys.push(key);
    }
    keys.forEach((key) => window.sessionStorage.removeItem(key));
  } catch { /* storage unavailable: nothing was remembered */ }
}

export const anything = (_v: unknown): _v is unknown => true;
export const isStringOrNull = (v: unknown): v is string | null => v === null || typeof v === "string";
/** A validator for a fixed set of strings. */
export const oneOf = <T extends string>(...allowed: readonly T[]) => (v: unknown): v is T => typeof v === "string" && (allowed as readonly string[]).includes(v);

export function usePersistedState<T>(key: string, initial: T, isValid: (v: unknown) => v is T): [T, (next: T | ((prev: T) => T)) => void] {
  const initialRef = useRef(initial);
  const [value, setValue] = useState<T>(() => readPersisted(key, initial, isValid));
  useEffect(() => writePersisted(key, value, initialRef.current), [key, value]);
  const set = useCallback((next: T | ((prev: T) => T)) => setValue(next), []);
  return [value, set];
}
