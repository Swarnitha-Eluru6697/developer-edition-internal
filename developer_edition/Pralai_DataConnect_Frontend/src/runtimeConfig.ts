/*
 * FE-GAP-01: where the backend URL comes from, decided in ONE pure function so the rule is testable and cannot be
 * bypassed by a code path that forgets it.
 *
 *   FEDERATED      the host's `apiBase` is REQUIRED and is the only source. If it is missing or blank the result is a
 *                  configuration error (rendered by ConfigGate) — never the development backend, never a value left in
 *                  localStorage.
 *   STANDALONE_DEV host props win; otherwise whatever Settings last saved; otherwise the local development default below.
 *                  A developer running `npm run dev` with no host needs it to work at all.
 *
 * The tenant is NOT configuration: the backend takes it from the signed ticket (Envoy's x-tenant-id) and GET /me reports
 * it. A host or a developer cannot point DataConnect at another tenant.
 *
 * The development default lives here and nowhere else (tests/unit/runtime-config.spec.ts scans for it).
 */
import type { RuntimeMode } from "./runtimeMode";

export const STANDALONE_DEV_BASE = "http://localhost:8001/api/v1/dataconnect";

export type HostConfigField = "apiBase";

export interface RuntimeConfigInput {
  mode: RuntimeMode;
  apiBase?: unknown;
  /** Dev-only persisted value (Settings). Ignored outside STANDALONE_DEV, even if a caller passes it. */
  storedBase?: string | null;
}

export type RuntimeConfig =
  | { ok: true; baseUrl: string; hostControlled: boolean }
  | { ok: false; missing: HostConfigField[] };

/** A usable host-supplied string, trimmed; anything else (undefined, null, "", "  ", a number, an object) is "not provided". */
function provided(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function resolveRuntimeConfig({ mode, apiBase, storedBase }: RuntimeConfigInput): RuntimeConfig {
  const hostBase = provided(apiBase);

  if (mode === "FEDERATED") {
    if (!hostBase) return { ok: false, missing: ["apiBase"] };
    return { ok: true, baseUrl: hostBase, hostControlled: true };
  }

  return {
    ok: true,
    baseUrl: hostBase || provided(storedBase) || STANDALONE_DEV_BASE,
    hostControlled: Boolean(hostBase),
  };
}

/** The one-line description of a configuration error, also what is reported to the host's onError. */
export function configurationErrorMessage(missing: HostConfigField[]): string {
  return `DataConnect is not configured: the host did not provide ${missing.join(" and ")}.`;
}
