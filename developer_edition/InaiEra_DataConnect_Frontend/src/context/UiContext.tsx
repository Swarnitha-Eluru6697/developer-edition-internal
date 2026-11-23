/*
 * Small per-mount UI store shared by the Dashboard, Monitoring and Pipelines pages:
 *   selected  — the connector Monitoring is showing. The Dashboard's "Metrics →" and the Pipelines' "View metrics"
 *               set it, then navigate to Monitoring. Remembered in this browser, so a page refresh keeps it (a connector
 *               that no longer exists is dropped by Monitoring itself).
 *
 * One store PER MOUNT (created by <UiProvider>), not a module singleton: two DataConnect mounts on one host page
 * must not see each other's state (same rule as the persisted page — see DataConnectApp.tsx).
 */
import React, { createContext, useCallback, useContext, useMemo, useRef, useSyncExternalStore } from "react";

const LS_SELECTED = "dc_selected_connector";

/** The connector chosen before the last refresh, or null. Storage can be missing or throw, so never trust it. */
export function readStoredSelected(): string | null {
  try { return localStorage.getItem(LS_SELECTED) || null; } catch { return null; }
}

function writeStoredSelected(selected: string | null): void {
  try { if (selected) localStorage.setItem(LS_SELECTED, selected); else localStorage.removeItem(LS_SELECTED); } catch { /* not remembered */ }
}

export interface UiState {
  selected: string | null;
}

interface Store {
  get: () => UiState;
  set: (patch: Partial<UiState>) => void;
  subscribe: (listener: () => void) => () => void;
}

export function createUiStore(initial?: Partial<UiState>): Store {
  let state: UiState = { selected: null, ...initial };
  const listeners = new Set<() => void>();
  return {
    get: () => state,
    set: (patch) => {
      state = { ...state, ...patch };
      listeners.forEach((l) => l());
    },
    subscribe: (listener) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
  };
}

const UiStoreContext = createContext<Store | null>(null);

export function UiProvider({ children, initial }: { children: React.ReactNode; initial?: Partial<UiState> }) {
  const ref = useRef<Store | null>(null);
  if (!ref.current) ref.current = createUiStore({ selected: readStoredSelected(), ...initial });
  return <UiStoreContext.Provider value={ref.current}>{children}</UiStoreContext.Provider>;
}

function useStore(): Store {
  const store = useContext(UiStoreContext);
  if (!store) throw new Error("useUi must be used inside <UiProvider>");
  return store;
}

/** Subscribe to one slice. `select` must return a primitive (or a stable reference), never a fresh object. */
export function useUi<T>(select: (s: UiState) => T): T {
  const store = useStore();
  return useSyncExternalStore(store.subscribe, () => select(store.get()), () => select(store.get()));
}

export interface UiActions {
  selectConnector: (name: string | null) => void;
}

export function useUiActions(): UiActions {
  const store = useStore();
  const selectConnector = useCallback((selected: string | null) => { writeStoredSelected(selected); store.set({ selected }); }, [store]);
  return useMemo(() => ({ selectConnector }), [selectConnector]);
}
