/**
 * WorkspaceApp — the component the Control Portal federates in (exposed as `workspace_mfe/WorkspaceApp` via remoteEntry.js).
 *
 * Self-contained, like DataConnect and UCS: it brings its own Redux store and renders its own sidebar and header, so nothing in it
 * touches the host's URL or router. The host passes:
 *    apiBase — the same-origin tunnel path to the Workspace API (e.g. /control_plane/api/dataplane/workspace-api). Relative, so the
 *              portal's session cookie rides along, the BFF mints the data-plane ticket and Envoy injects the identity headers.
 *    orgId, canEdit — part of the shared remote contract; the API enforces access itself, so they are not needed here yet.
 *    roleBadges — a display-only strip of Workspace role names drawn by the portal; shown at the right of the header, on one line. It is
 *              never inspected: access stays permission-based (GET /api/v1/me). Passing it (even as null) means "hosted by the portal":
 *              the portal's left rail then carries the notification bell and the user's profile, so the header leaves both out.
 *    viewPermissions — "view as role": the permission IDs of the one role the user picked in the portal's role strip, or null /
 *              undefined for "All roles" (and from older portals). The UI shows what held ∩ viewPermissions opens, never more;
 *              switching re-renders in place, and a page no longer allowed falls back to Workspace. UX only, never sent to the API.
 *    accountMenu — deprecated (older portals only): the host's account menu, still drawn at the right of the header when given.
 *
 * The product mark ("InAiEra Workspace · Data Platform") stays at the top of the Workspace sidebar, right beside the portal rail's
 * InaiEra logo; the app never draws a second InaiEra logo.
 *
 * What main.tsx does before the first render and why it is skipped here: there is no `#token=` handoff to exchange (the portal owns
 * sign-in) and no /config.json to read (it only holds the control-plane login URL, which an embedded app never redirects to).
 */
import type { CSSProperties, ReactNode } from "react";
import { Provider } from "react-redux";
import { setRuntime } from "./api/apiBase";
import { App } from "./app/App";
import { ErrorBoundary } from "./app/ErrorBoundary";
import { lockHiddenScroll } from "./app/lockHiddenScroll";
import { ViewPermissionsProvider } from "./rbac/ViewPermissionsProvider";
import { store } from "./store";

export interface WorkspaceAppProps {
  apiBase?: string;
  orgId?: string;
  canEdit?: boolean;
  /**
   * Display-only strip of this app's role names, drawn by the portal (the user's own roles highlighted). Shown at the right of the
   * header and never inspected. When given, the app is hosted by the portal: no header bell or avatar (the portal rail has them).
   */
  roleBadges?: ReactNode;
  /**
   * "View as role": the permission IDs of the role the user chose in the portal's role strip. null / undefined = no narrowing ("All
   * roles", or an older portal). Narrows what is shown (held ∩ viewPermissions), never widens it; UX only, never sent to the API.
   */
  viewPermissions?: string[] | null;
  /** Deprecated, older portals only: the host's account menu (profile, sign out), shown at the right of the header when given. */
  accountMenu?: ReactNode;
}

// Typefaces and the icon font are bundled with the app (App.tsx imports them), so nothing is linked from a third-party service.

// index.html's base styles (design tokens, base font, page background), applied to the app's own root instead of html/body so they
// don't restyle the portal around it.
const rootStyle = {
  "--disp": 'Inter, system-ui, sans-serif',
  "--body": 'Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
  "--mono": '"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
  height: "100%",
  background: "var(--ws-bg, #F8FAF9)",
  fontFamily: "var(--body)",
  color: "var(--ws-text, #14201B)",
  fontSize: 14,
  WebkitFontSmoothing: "antialiased",
} as CSSProperties;

let started = false;

/** One-time page set-up that main.tsx does for the standalone app. */
function startOnce(): void {
  if (started) return;
  started = true;
  lockHiddenScroll();
}

export default function WorkspaceApp({ apiBase = "", accountMenu = null, roleBadges, viewPermissions }: WorkspaceAppProps) {
  // Set before the first render so the first API call (/auth/me) already goes through the tunnel.
  setRuntime({ apiBase, embedded: true, accountMenu, roleBadges });
  startOnce();
  return (
    <div style={rootStyle}>
      <ErrorBoundary>
        <Provider store={store}>
          <ViewPermissionsProvider viewPermissions={viewPermissions}>
            <App />
          </ViewPermissionsProvider>
        </Provider>
      </ErrorBoundary>
    </div>
  );
}
