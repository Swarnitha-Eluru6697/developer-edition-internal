/**
 * Workspace authorisation in the UI — by PERMISSION only (platform permission contract, 01-CONTRACT.md).
 *
 * The permission IDs are GENERATED (./generated/permissions.ts, from openapi/permissions.json, a copy of the workspace backend's
 * permissions.json; `npm run codegen:rbac`). Who holds which permission is data owned by the Admin Portal and arrives per user from
 * GET /api/v1/me, so nothing here maps people, groups or job titles to permissions.
 *
 * The backend checks the same IDs on every route; this only drives what the UI shows, hides or disables. The least-privileged
 * state is "no permissions": unknown, loading or failed = nothing.
 */
import { PERMISSIONS, type Permission } from "./generated/permissions";

export { PERMISSIONS, PERMISSION_DESCRIPTIONS, PERMISSION_LABELS, PERMISSIONS_VERSION } from "./generated/permissions";
export type { Permission };

/** "May open Workspace at all". Without it the app shows the no-access screen. */
export const ACCESS_PERMISSION: Permission = "workspace:access:view";

const KNOWN = new Set<string>(PERMISSIONS);

export function isPermission(value: unknown): value is Permission {
  return typeof value === "string" && KNOWN.has(value);
}

/** The Workspace permissions in `raw` (anything that is not a known ID — another app's, or junk — is dropped), deduplicated and in
 *  permissions.json order. Not an array = no permissions. */
export function toPermissions(raw: unknown): Permission[] {
  if (!Array.isArray(raw)) return [];
  const held = new Set(raw.filter(isPermission));
  return PERMISSIONS.filter((p) => held.has(p));
}

/** Exact match: there are no wildcards or implied permissions. */
export function hasPermission(held: readonly string[], permission: Permission): boolean {
  return held.includes(permission);
}

/** True when `held` contains at least one of `anyOf` (an empty `anyOf` = nothing required). */
export function hasAnyPermission(held: readonly string[], anyOf: Permission | readonly Permission[] | undefined): boolean {
  if (anyOf === undefined) return true;
  const list: readonly Permission[] = typeof anyOf === "string" ? [anyOf] : anyOf;
  return list.length === 0 || list.some((p) => held.includes(p));
}

/**
 * The permissions the UI acts on when the portal's "view as role" picker is set: `held` ∩ `viewAs`. `viewAs` null / undefined = no
 * narrowing (`held` itself, same reference). Never widens: a permission not in `held` stays out even when `viewAs` lists it.
 */
export function narrowPermissions(held: readonly Permission[], viewAs: readonly string[] | null | undefined): readonly Permission[] {
  if (viewAs == null) return held;
  const view = new Set(viewAs);
  return held.filter((p) => view.has(p));
}
