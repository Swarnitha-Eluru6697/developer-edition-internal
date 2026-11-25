// A screen now remembers its place in sessionStorage (src/shared/usePersistedState.ts), so one test's place must not leak into the next.
import { afterEach } from "vitest";

afterEach(() => {
  try { window.sessionStorage.clear(); } catch { /* not a browser-like environment */ }
});
