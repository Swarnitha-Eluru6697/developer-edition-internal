/**
 * Where the API lives, set by the host when the Workspace runs as a Module-Federation micro-frontend inside the Control Portal.
 *
 *   standalone : API_BASE = "" — the SPA is served next to the API, so `/api/v1/*` and `/ws/*` are same-origin; the session comes
 *                from the `#token=` handoff (api/auth.ts).
 *   embedded   : API_BASE = the tunnel path (e.g. /control_plane/api/dataplane/workspace-api), still same-origin with the portal, so
 *                its session cookie rides along, the BFF mints the data-plane ticket and Envoy injects the identity headers. There is
 *                no token handoff and no control-plane redirect: the portal owns sign-in.
 *
 * Portal chrome: a current portal passes `roleBadges` (a display-only strip of role names, drawn in the header) and draws the bell and
 * the user's profile in its own left rail, so the Workspace header bell and avatar are left out (`hasPortalChrome`). An older portal
 * passes `accountMenu` instead, which is still drawn where the avatar sits.
 */
import type { ReactNode } from "react";

let API_BASE = "";
let EMBEDDED = false;
let ACCOUNT_MENU: ReactNode = null;
let ROLE_BADGES: ReactNode | undefined = undefined;

/**
 * `roleBadges` is applied whenever the key is present, even as `undefined`, because `undefined` itself means "not hosted by a current
 * portal" (standalone chrome).
 */
export function setRuntime(opts: { apiBase?: string; embedded?: boolean; accountMenu?: ReactNode; roleBadges?: ReactNode }): void {
  if (opts.apiBase != null) API_BASE = opts.apiBase.replace(/\/+$/, "");
  if (opts.embedded != null) EMBEDDED = opts.embedded;
  if (opts.accountMenu !== undefined) ACCOUNT_MENU = opts.accountMenu;
  if ("roleBadges" in opts) ROLE_BADGES = opts.roleBadges;
}

/** Prefix a service-relative path (e.g. "/api/v1/...") with the runtime API base. */
export const apiUrl = (path: string): string => API_BASE + path;

/** True when running inside the host portal (identity handled by Envoy, sign-in by the portal). */
export const isEmbedded = (): boolean => EMBEDDED;

/** The host portal's account menu (profile, user management, sign out), drawn in place of the Workspace user menu. Null standalone. */
export const hostAccountMenu = (): ReactNode => ACCOUNT_MENU;

/**
 * True when a current portal hosts the app (it passed `roleBadges`): the portal rail then carries the bell and the profile, so the
 * header leaves them out. Never used for access decisions — those come from the caller's permissions (rbac/).
 */
export const hasPortalChrome = (): boolean => ROLE_BADGES !== undefined;

/** The portal's role-badge strip, display only (never inspected). Undefined when not hosted by a current portal. */
export const hostRoleBadges = (): ReactNode | undefined => ROLE_BADGES;
