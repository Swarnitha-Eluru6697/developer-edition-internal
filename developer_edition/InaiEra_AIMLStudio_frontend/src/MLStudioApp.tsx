/*
 * MLStudioApp — the component the Control Portal federates in
 * (exposed as `mlstudio_mfe/MLStudioApp` via remoteEntry.js).
 *
 * Self-contained, the same contract Catalog and DataConnect use: it brings its
 * own providers and an internal MemoryRouter, so its routing lives inside the
 * panel and never fights the host for the address bar. The host passes:
 *    apiBase  — the same-origin tunnel path to the ML Studio API
 *               (e.g. /control_plane/api/dataplane/mlstudio), relative so the
 *               session cookie rides and the BFF mints the data-plane ticket
 *    embedded — hide ML Studio's own sidebar/topbar when the host supplies chrome
 *    roleBadges — the portal's display-only strip of ML Studio role names, drawn
 *               at the right of the top bar. Passing it means "hosted": the
 *               portal's rail carries the bell and the user, so ML Studio's own
 *               bell and avatar are not drawn (the Alerts page stays)
 *    viewPermissions — "view as role": the permission IDs of the one role the
 *               user chose in the portal's role strip, or null/undefined for
 *               "All roles". Narrows what is shown to /api/me ∩ this; never
 *               widens, never sent to the backend (UX only)
 *    accountMenu — older portals only: their account menu, shown in place of
 *               ML Studio's own avatar at the right of the top bar
 *
 * Who the user is and what they may do is NOT a prop: ML Studio reads it from
 * `${apiBase}/api/me`, which the backend answers from the Envoy-verified ticket.
 */
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { AppShell } from "./App";
import { PermissionsProvider } from "./context/PermissionsContext";
import { ToastProvider } from "./context/ToastContext";
import { setRuntime } from "./runtimeConfig";
import "./styles/global.css";

export interface MLStudioAppProps {
  /** Same-origin tunnel base to the ML Studio API. */
  apiBase?: string;
  /** Hide ML Studio's own chrome when the host supplies it. */
  embedded?: boolean;
  /**
   * Which ML Studio route the panel opens on. The host's rail has separate
   * entries (Models, Datasets) that must land on different screens inside the
   * one remote — without this they would all open the dashboard and the rail
   * selection would appear to do nothing.
   */
  initialPath?: string;
  /**
   * The portal's strip of ML Studio role names (the user's own highlighted).
   * Display only — nothing is derived from it; access comes from /api/me.
   * When given (hosted), ML Studio drops its own bell and avatar.
   */
  roleBadges?: ReactNode;
  /**
   * "View as role": the permission IDs of the role the user chose to view
   * ML Studio as. Pages, nav and actions show from me.permissions ∩ this —
   * never more than /api/me grants. `null`/`undefined` = no narrowing (older
   * portals send nothing). UX only: never sent to the backend.
   */
  viewPermissions?: string[] | null;
  /** Backward compatibility: an older host's account menu (profile, user
   *  management, sign out), shown in place of ML Studio's own avatar. */
  accountMenu?: ReactNode;
}

export default function MLStudioApp({ apiBase = "", embedded = true, initialPath = "/", roleBadges, viewPermissions, accountMenu = null }: MLStudioAppProps) {
  // Set before the first render so the API client (and the /api/me read) use
  // the tunnel base. The browser sends no identity of its own — Envoy supplies
  // it from the validated ticket.
  setRuntime({ apiBase, embedded, accountMenu });
  return (
    <div className="mlstudio-root">
      <PermissionsProvider viewPermissions={viewPermissions}>
        <ToastProvider>
          <MemoryRouter initialEntries={[initialPath]}>
            <AppShell embedded={embedded} roleBadges={roleBadges} />
          </MemoryRouter>
        </ToastProvider>
      </PermissionsProvider>
    </div>
  );
}
