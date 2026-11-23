/**
 * Permission hooks for UI gating. Permissions are loaded once from GET /api/v1/me (./api.ts; RTK Query shares the one request
 * between every caller). Until it answers, or if it fails, the user holds NO permissions: gated things stay hidden.
 *
 * "View as role" (./viewAs.ts): when the portal passes `viewPermissions`, every hook here returns the EFFECTIVE set, the held
 * permissions narrowed to that list (never widened). This is the one place the narrowing happens, so every nav item, page and button
 * follows it.
 *
 * None of this is the security boundary: the backend checks the same permission on every route (403 without it).
 */
import { useMemo } from "react";
import { useGetMyPermissionsQuery } from "./api";
import { hasAnyPermission, narrowPermissions, toPermissions, type Permission } from "./permissions";
import { useViewPermissions } from "./viewAs";

export type PermissionsStatus = "loading" | "ready" | "error";

export interface PermissionsState {
  /** The permissions the UI acts on: `heldPermissions`, narrowed to the portal's `viewPermissions` when given. */
  permissions: readonly Permission[];
  /**
   * Every Workspace permission the caller holds, ignoring "view as role" (empty while loading or after an error). Only for "may the
   * app open at all" (workspace:access:view); everything the UI shows or hides uses `permissions`.
   */
  heldPermissions: readonly Permission[];
  status: PermissionsStatus;
  /** The RTK Query error when `status` is "error". */
  error: unknown;
}

const NONE: readonly Permission[] = Object.freeze([]);

export function usePermissionsState(): PermissionsState {
  const { data, error, isError } = useGetMyPermissionsQuery();
  const raw = isError ? undefined : data?.permissions;
  // A stable array for the same response, so effects that depend on it do not re-run on every render.
  const heldPermissions = useMemo<readonly Permission[]>(() => (raw ? toPermissions(raw) : NONE), [raw]);
  const viewPermissions = useViewPermissions();
  const permissions = useMemo(() => narrowPermissions(heldPermissions, viewPermissions), [heldPermissions, viewPermissions]);
  const status: PermissionsStatus = isError ? "error" : data ? "ready" : "loading";
  return { permissions, heldPermissions, status, error };
}

/** The caller's effective Workspace permissions (narrowed by "view as role"); empty while loading or on error. */
export function usePermissions(): readonly Permission[] {
  return usePermissionsState().permissions;
}

/** Whether the caller holds `permission` (exact match). False while loading or on error. */
export function useHasPermission(permission: Permission): boolean {
  return usePermissions().includes(permission);
}

/** Whether the caller holds at least one of `anyOf`. */
export function useHasAnyPermission(anyOf: readonly Permission[]): boolean {
  return hasAnyPermission(usePermissions(), anyOf);
}
