/**
 * Extracts the plain identity fields Envoy injects, once the caller has
 * already been proven to be Envoy via the gateway secret. These field
 * names are the platform-wide contract (01-CONTRACT.md section 4) — every
 * service reads exactly these, never a client-supplied equivalent.
 */
import { splitMulti } from "./parse";

export interface GatewayIdentity {
  username: string;
  groups: string[];
  tenantId: string;
  /** x-user-id (the token's sub): the immutable user id. "" if absent. */
  userId: string;
  /** x-user-permissions: what the user may do. The only input to authorisation. */
  permissions: string[];
  /** x-user-roles: display/audit ONLY. Never authorise on it. */
  roles: string[];
}

export function extractIdentity(headers: {
  username: string | null | undefined;
  groups: string | null | undefined;
  tenantId: string | null | undefined;
  userId?: string | null;
  permissions?: string | null;
  roles?: string | null;
}, defaultTenantId: string): GatewayIdentity | null {
  if (!headers.username) return null;
  return {
    username: headers.username,
    groups: splitMulti(headers.groups),
    tenantId: headers.tenantId || defaultTenantId,
    userId: (headers.userId || "").trim(),
    permissions: [...new Set(splitMulti(headers.permissions))],
    roles: splitMulti(headers.roles),
  };
}

/** True if the identity holds `permission` exactly. No identity = false. */
export function hasPermission(identity: Pick<GatewayIdentity, "permissions"> | null | undefined, permission: string): boolean {
  return !!identity && Array.isArray(identity.permissions) && identity.permissions.includes(permission);
}
