import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { apiUrl } from "../runtimeConfig";
import { canReachPath } from "../permissions";

/**
 * The signed-in user's permissions, read once from GET <apiBase>/api/me.
 *
 * The browser sends no identity of its own: Envoy verifies the user's ticket
 * and the backend answers with { user, userId, tenant, permissions } from it.
 * Until that answer arrives — or if it fails — the user holds nothing, so every
 * page and action stays hidden (unknown = hidden, never "allowed by default").
 *
 * "View as role": the host may pass `viewPermissions` — the permission IDs of
 * one of the user's own roles. `can`/`canReach` then answer from
 * me.permissions ∩ viewPermissions, so every nav item, page and action follows
 * the chosen view. It only ever narrows (a permission the user lacks never
 * appears) and is UX only: it is never sent to the backend, which still
 * authorises on the ticket. null/undefined = the full set. `me.permissions`
 * stays the user's full set.
 */
export interface Me {
  user: string;
  userId: string;
  tenant: string;
  permissions: string[];
}

type Status = "loading" | "ready" | "error";

interface PermissionsValue {
  status: Status;
  me: Me | null;
  /** True only once loaded and the permission is held. */
  can: (permission: string) => boolean;
  /** True when the user may open `pathname` (see permissions.ts). */
  canReach: (pathname: string) => boolean;
  error: string | null;
}

const PermissionsContext = createContext<PermissionsValue>({
  status: "loading",
  me: null,
  can: () => false,
  canReach: () => false,
  error: null,
});

/**
 * The permissions the UI shows from: the held set, narrowed to `view` when one
 * is given. Never widens — only IDs already held can survive.
 */
export function effectivePermissions(held: readonly string[], view?: readonly string[] | null): string[] {
  if (!view) return [...held];
  const allowed = new Set(view);
  return held.filter((p) => allowed.has(p));
}

export function PermissionsProvider({ children, viewPermissions }: { children: ReactNode; viewPermissions?: string[] | null }) {
  const [status, setStatus] = useState<Status>("loading");
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetch(apiUrl("/me"), { headers: { Accept: "application/json" } })
      .then(async (res) => {
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error((body as { error?: string }).error || `HTTP ${res.status}`);
        return body as Partial<Me>;
      })
      .then((body) => {
        if (!alive) return;
        setMe({
          user: typeof body.user === "string" ? body.user : "",
          userId: typeof body.userId === "string" ? body.userId : "",
          tenant: typeof body.tenant === "string" ? body.tenant : "",
          permissions: Array.isArray(body.permissions) ? body.permissions.filter((p): p is string => typeof p === "string") : [],
        });
        setStatus("ready");
      })
      .catch((e) => {
        if (!alive) return;
        setMe(null);
        setError(e instanceof Error ? e.message : String(e));
        setStatus("error");
      });
    return () => { alive = false; };
  }, []);

  // Keyed on content, not identity: the host re-renders with a fresh array.
  const viewKey = viewPermissions ? JSON.stringify(viewPermissions) : null;
  const held = useMemo(
    () => new Set(status === "ready" ? effectivePermissions(me?.permissions ?? [], viewKey ? (JSON.parse(viewKey) as string[]) : null) : []),
    [status, me, viewKey],
  );
  const can = useCallback((p: string) => held.has(p), [held]);
  const canReach = useCallback((pathname: string) => canReachPath(can, pathname), [can]);

  const value = useMemo(() => ({ status, me, can, canReach, error }), [status, me, can, canReach, error]);
  return <PermissionsContext.Provider value={value}>{children}</PermissionsContext.Provider>;
}

export function usePermissions() {
  return useContext(PermissionsContext);
}
