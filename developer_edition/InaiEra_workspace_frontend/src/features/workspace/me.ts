import type { CurrentUser, MeResponse } from "../../types";

/**
 * GET /auth/me nests identity under `claims` (gateway identity) and `user` (the local user mirror) — see app/api/auth.py:me.
 * Flatten it into the CurrentUser view the SPA uses, so email/display_name are never undefined. Permissions come from
 * GET /api/v1/me instead (src/rbac/hooks.ts).
 */
export function flattenMe(raw: MeResponse): CurrentUser {
  const email = raw.claims.email ?? raw.user.email ?? "";
  return {
    user_id: raw.claims.user_id,
    org_id: raw.claims.org_id,
    email,
    display_name: raw.user.display_name ?? email.split("@")[0],
  };
}
