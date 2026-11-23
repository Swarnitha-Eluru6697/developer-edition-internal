/*
 * FE-BUG-02: an explicit, named runtime mode — the one boundary that decides
 * whether localStorage-persisted dev state (the backend URL; see
 * AppContext.tsx's LS_BASE) is ever consulted at all.
 *
 * STANDALONE_DEV is the only mode that reads it. FEDERATED treats the host's
 * own props (apiBase) as the exclusive source of truth: it never falls back to
 * whatever a previous local dev session happened to leave in this browser's
 * storage. Who the user is and what they may do is never local state in
 * either mode — it is the backend's GET /me.
 *
 * import.meta.env.DEV is a Vite build-time constant, baked into the bundle
 * at build time — always false in a `vite build` output (see main.tsx),
 * regardless of what server later serves that build or what origin/port it
 * happens to share with a previous `npm run dev` session. That's what makes
 * RUNTIME_MODE a real security boundary rather than a runtime toggle: a
 * production bundle can never be talked into STANDALONE_DEV mode after the
 * fact — not by a query param, not by anything sitting in storage.
 */
export type RuntimeMode = "FEDERATED" | "STANDALONE_DEV";

export const RUNTIME_MODE: RuntimeMode = import.meta.env.DEV ? "STANDALONE_DEV" : "FEDERATED";
