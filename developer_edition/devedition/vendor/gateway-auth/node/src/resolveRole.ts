/**
 * DEPRECATED (v1.1): authorise on permissions (requirePermission /
 * hasPermission), not role names. Removed in the next release.
 *
 * Resolves which of a service's OWN roles apply to a user, from Envoy's
 * X-User-Roles header (the user's full set of Keycloak realm roles, which
 * legitimately may include roles for other products too).
 *
 * The ALGORITHM is standardized and identical for every service: split on
 * comma, keep only roles this service recognizes, pick the highest tier if
 * more than one matches, default to the least-privileged role if none
 * match at all — never default to admin. The actual ROLE NAMES and their
 * TIER ORDERING are service-specific and passed in as config, since ML
 * Studio's roles are not Workspace's roles are not Agent Studio's roles.
 *
 * This mirrors ml-studio-app's resolveRole() exactly, generalized to take
 * any service's role vocabulary rather than hardcoding ML Studio's four.
 */
export interface RoleConfig<TRole extends string> {
  /** Every role this service recognizes, in ANY order. */
  allRoles: readonly TRole[];
  /** Tier per role — higher number wins when a user matches more than one. */
  tiers: Record<TRole, number>;
  /** Used when X-User-Roles contains none of this service's roles. Must be the lowest tier — never the highest. */
  defaultRole: TRole;
}

export { splitMulti } from "./parse";
import { splitMulti } from "./parse";

let warned = false;
export function resolveRole<TRole extends string>(rolesHeader: string | null | undefined, config: RoleConfig<TRole>): TRole {
  if (!warned) {
    warned = true;
    process.emitWarning("resolveRole/RoleConfig are deprecated: authorise on permissions (requirePermission) instead of role names.", "DeprecationWarning");
  }
  const claimed = splitMulti(rolesHeader);
  const matched = claimed.filter((r): r is TRole => (config.allRoles as readonly string[]).includes(r));
  if (matched.length === 0) return config.defaultRole;
  return matched.reduce((best, r) => (config.tiers[r] > config.tiers[best] ? r : best), matched[0]);
}
