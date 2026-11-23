// AgenticWorkflowApp — the component the Control Portal federates in (exposed as
// `agenticworkflow_mfe/AgenticWorkflowApp` via remoteEntry.js).
//
// Self-contained, like DataConnect and UCS: it brings its own providers and an
// internal MemoryRouter, so its routing lives inside the panel and never fights
// the host's URL. The host passes:
//    apiBase — the same-origin tunnel path to the API (relative, so the session
//              cookie rides and the BFF mints the data-plane ticket; e.g.
//              /control_plane/api/dataplane/agenticworkflow-api). Envoy injects
//              the identity headers, so the app sends none.
//    roleBadges — the portal's display-only strip of this app's role names (the
//              user's own highlighted), drawn at the right of the top bar. Its
//              presence means the app is hosted: the portal's rail then carries
//              the notification bell and the account menu, so the top bar drops
//              its own. Never inspected — access still comes from /api/me's
//              permissions. Undefined when standalone.
//    viewPermissions — the portal's "view as role" choice: the permission IDs of
//              the one role the user picked in the role strip, or null /
//              undefined for "All roles" (and from older portals). Every
//              show/hide check uses /api/me's permissions intersected with it
//              (AppContext's effectivePermissions), so it can only hide, never
//              grant; a switch re-renders in place, and a page no longer
//              allowed redirects to the first one that is. UX only: never sent
//              to the API, which still authorises on the ticket.
//    accountMenu — older portals only: their account menu (profile, sign out),
//              shown in place of the user-initials avatar at the right of the
//              top bar. Still accepted so those portals keep working.
//    orgId, canEdit — part of the shared remote contract; the API enforces
//              access itself, so they are not needed here yet.
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { AppRoutes } from './App';
import { setApiBase } from './api';
import { setHostAccountMenu, setHostRoleBadges } from './host';
import './styles.css';

export interface AgenticWorkflowAppProps {
  apiBase?: string;
  orgId?: string;
  canEdit?: boolean;
  roleBadges?: ReactNode;
  accountMenu?: ReactNode;
  viewPermissions?: string[] | null;
}

// index.html isn't loaded when the app is federated, so its fonts have to be
// added here or every page falls back to the system typeface.
const FONTS_HREF =
  'https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap';

function ensureFonts() {
  if (document.querySelector(`link[href="${FONTS_HREF}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = FONTS_HREF;
  document.head.appendChild(link);
}

// "Launch" / "Docs" links open `#/app/<id>` or `#/docs/<type>` in a new tab,
// which lands on the portal's page for this app with that hash. Start there
// instead of the overview so those links keep working when federated.
function initialPath(): string {
  const hash = window.location.hash.replace(/^#/, '');
  return /^\/(app|docs)\//.test(hash) ? hash : '/';
}

export default function AgenticWorkflowApp({ apiBase = '', roleBadges, accountMenu = null, viewPermissions }: AgenticWorkflowAppProps) {
  // Set before the first render so the first API call already uses the tunnel.
  if (apiBase) setApiBase(apiBase);
  setHostAccountMenu(accountMenu);
  setHostRoleBadges(roleBadges);
  ensureFonts();
  return (
    <MemoryRouter initialEntries={[initialPath()]}>
      {/* A prop, not module state like the badges, so a switch re-renders
          every permission check without a remount. */}
      <AppRoutes viewPermissions={viewPermissions} />
    </MemoryRouter>
  );
}
