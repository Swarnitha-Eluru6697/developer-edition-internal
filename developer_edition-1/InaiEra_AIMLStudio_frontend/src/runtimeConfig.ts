// Runtime config injected by the host when ML Studio runs as a Module-Federation
// micro-frontend inside the Control Portal.
//
//   standalone dev : API_BASE="" (Vite proxies /api to the backend), embedded=false.
//   embedded (MFE) : API_BASE = the tunnel path (e.g. /control_plane/api/dataplane/mlstudio);
//                    embedded=true.
// In both, the SPA sends NO identity of its own: Envoy injects it from the
// validated ticket, and the user's permissions are read from GET /api/me.
//
// Deliberately module-level rather than React context: the API client is a
// plain module that pages import directly, and threading a provider through
// every call site would be a much larger change for no behavioural gain. The
// host sets this before its first render (see MLStudioApp).
import type { ReactNode } from "react";

let API_BASE = "";
let EMBEDDED = false;
let ACCOUNT_MENU: ReactNode = null;

export function setRuntime(opts: { apiBase?: string; embedded?: boolean; accountMenu?: ReactNode }): void {
  if (opts.apiBase != null) API_BASE = opts.apiBase.replace(/\/+$/, "");
  if (opts.embedded != null) EMBEDDED = opts.embedded;
  if (opts.accountMenu !== undefined) ACCOUNT_MENU = opts.accountMenu;
}

/** Prefix a service-relative path (e.g. "/models") with the runtime API base. */
export const apiUrl = (path: string): string => `${API_BASE}/api${path}`;

/** True when running inside the host portal (auth handled by Envoy, not the SPA). */
export const isEmbedded = (): boolean => EMBEDDED;

/** The host portal's account menu (profile, user management, sign out), drawn in
 *  place of ML Studio's own avatar at the right of the top bar. Null standalone. */
export const hostAccountMenu = (): ReactNode => ACCOUNT_MENU;
