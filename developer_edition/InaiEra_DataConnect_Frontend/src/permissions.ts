/*
 * DataConnect authorisation in the UI (contract docs/auth/01-CONTRACT.md). The UI never decides WHO may do what: the
 * backend's GET /me returns the caller's permissions (from the ticket, via Envoy), and pages/buttons are shown, hidden
 * or disabled from that list. No role name exists in this app — roles live in the Admin Portal.
 *
 * The permission IDs come from the backend's permissions.json, generated into types/permissions.generated.ts
 * (`npm run gen:access-contract`), so `can("dataconnect:...")` only type-checks for an ID the backend declares.
 * The backend is still the enforcement boundary (it 403s); hiding something here is convenience only.
 */
import { PERMISSION_IDS } from "./types/permissions.generated";

export { PERMISSION_IDS };
export type Permission = (typeof PERMISSION_IDS)[number];

/** GET /me — contract section 5.4. */
export interface Me {
  user: string;
  userId: string;
  tenant: string;
  permissions: string[];
}

/** The permission that lets someone open DataConnect at all (the portal's menu gate). */
export const ACCESS_PERMISSION: Permission = "dataconnect:access:view";

/** A permission check over a set the backend returned. Unknown / absent = no permission: least privilege is "nothing". */
export type Can = (permission: Permission) => boolean;

export function permissionChecker(permissions: readonly string[] | null | undefined): Can {
  const held = new Set(Array.isArray(permissions) ? permissions.filter((p): p is string => typeof p === "string") : []);
  return (permission) => held.has(permission);
}

/** Holds nothing — what the UI uses until /me has answered, or when it failed. */
export const CAN_NOTHING: Can = () => false;

/**
 * "View as role" (the portal's role strip): the host may pass the permission IDs of ONE of the user's roles to view DataConnect as.
 * The effective set is /me ∩ viewPermissions — it can only narrow, never widen: an ID the user does not hold stays absent however the
 * host lists it. `null` / `undefined` (and anything that is not an array, e.g. from an older portal) means no narrowing. UX only: the
 * backend still authorises on the ticket's permissions, and viewPermissions is never sent to it. Permission IDs only — never role names.
 */
export function effectivePermissions(
  held: readonly string[] | null | undefined,
  viewPermissions: readonly string[] | null | undefined,
): string[] {
  const mine = Array.isArray(held) ? held.filter((p): p is string => typeof p === "string") : [];
  if (!Array.isArray(viewPermissions)) return mine;
  const view = new Set(viewPermissions.filter((p): p is string => typeof p === "string"));
  return mine.filter((p) => view.has(p));
}
