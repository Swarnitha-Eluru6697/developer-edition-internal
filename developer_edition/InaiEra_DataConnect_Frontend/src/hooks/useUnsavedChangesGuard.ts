/*
 * FE-BUG-13: registers the currently-open drawer form's "do I have unsaved
 * changes" check with the one shared modal context (AppContext.tsx), so
 * every way of closing the drawer — the × icon, the backdrop click, a
 * form's own Cancel button — goes through the same "Unsaved changes"
 * confirmation instead of each needing its own bespoke prompt wired in.
 *
 * `isDirty` is read through a ref rather than baked into the registered
 * guard function directly, so the guard always reports the form's current
 * dirty state — including at browser beforeunload time, which can fire long
 * after this hook's own effect last ran — without needing to re-register
 * (and briefly un-register) the guard on every keystroke.
 */
import { useEffect, useRef } from "react";
import { useApp } from "../context/AppContext";

export function useUnsavedChangesGuard(isDirty: boolean, message?: string): void {
  const { setCloseGuard } = useApp();
  const dirtyRef = useRef(isDirty);
  dirtyRef.current = isDirty;

  useEffect(() => {
    setCloseGuard(() => dirtyRef.current, message);
    return () => setCloseGuard(null);
  }, [setCloseGuard, message]);
}
