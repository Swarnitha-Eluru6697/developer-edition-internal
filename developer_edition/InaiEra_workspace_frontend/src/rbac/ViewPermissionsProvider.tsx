/** Puts the portal's `viewPermissions` ("view as role", see ./viewAs.ts) into React context for rbac/hooks.ts. */
import { useMemo, type ReactNode } from "react";
import { ViewPermissionsContext } from "./viewAs";

export function ViewPermissionsProvider({ viewPermissions, children }: { viewPermissions?: readonly string[] | null; children: ReactNode }) {
  // The portal may hand over a fresh array on every render; key on the content so consumers' memos and effects only re-run on a
  // real switch.
  const key = Array.isArray(viewPermissions) ? JSON.stringify(viewPermissions.filter((p) => typeof p === "string")) : null;
  const value = useMemo<readonly string[] | null>(() => (key === null ? null : Object.freeze(JSON.parse(key) as string[])), [key]);
  return <ViewPermissionsContext.Provider value={value}>{children}</ViewPermissionsContext.Provider>;
}
