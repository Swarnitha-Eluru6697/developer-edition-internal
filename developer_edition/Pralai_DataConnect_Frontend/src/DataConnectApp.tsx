/*
 * DataConnectApp — the component the Control Portal federates in.
 *
 * Self-contained: it renders its own sidebar + the DataConnect pages, so it
 * works identically standalone (npm run dev) and mounted inside the host. The
 * host passes:
 *    apiBase     — the tunnel route to the backend (same-origin, relative). REQUIRED when federated (FE-GAP-01).
 *    embedded    — hide DataConnect's own sidebar when the host supplies chrome (the logo then moves to the top bar's left end)
 *    onOperation — callback for write telemetry (feeds the host activity bell)
 *    roleBadges  — the portal's display-only strip of DataConnect role names, drawn at the right of the top bar. Its presence
 *                  means "hosted by the portal": the portal's rail then carries the bell and the profile, so DataConnect draws
 *                  no avatar of its own. Never inspected — access still comes from GET /me.
 *    accountMenu — DEPRECATED (older portals): the host's account menu, shown in place of DataConnect's own avatar
 *    viewPermissions — "view as role": the permission IDs of the one role the user picked in the portal's role strip, or
 *                  null/undefined for all their roles. Narrows what is SHOWN to /me ∩ viewPermissions (never wider than /me);
 *                  applied once, in AppContext's `can`. UX only — never sent to the backend, which still authorises on the ticket.
 *
 * Who the user is, their tenant and what they may do are NOT props: they come from the backend's GET /me (the signed
 * ticket, via Envoy). Pages and buttons are shown from those permissions; the backend enforces them.
 */
import React, { useState, useCallback, useEffect } from "react";
import "./styles.css";
import { PralaiLockup } from "./components/PralaiLogo";
import { AppProvider, useApp } from "./context/AppContext";
import { UiProvider } from "./context/UiContext";
import Modal from "./components/Modal";
import Toasts from "./components/Toasts";
import ErrorBoundary from "./components/ErrorBoundary";
import HeaderSearch from "./components/HeaderSearch";
import NavIcon from "./components/NavIcons";
import { ACCESS_PERMISSION, type Can, type Permission } from "./permissions";
import type { ApiOperation } from "./api";
import type { PageFocusProps, SearchTarget } from "./searchTarget";

import Dashboard from "./pages/Dashboard";
import Monitoring from "./pages/Monitoring";
import Connections from "./pages/Connections";
import Pipelines from "./pages/Pipelines";
import Schemas from "./pages/Schemas";
import Quality from "./pages/Quality";
import Lineage from "./pages/Lineage";
import Transforms from "./pages/Transforms";
import Templates from "./pages/Templates";
import Alerts, { ALERTS_FROM_NAV_KEY } from "./pages/Alerts";
import { useGetAlertCountsQuery } from "./store/apiSlice";
import { ALERT_COUNTS_POLL_MS } from "./pollingPolicy";
import FastTooltip from "./components/FastTooltip";
import { DeveloperUserMenu, inDeveloperShell } from "./components/DeveloperUserMenu";

export type PageId = "dash" | "mon" | "alrt" | "conn" | "pip" | "sch" | "qual" | "lin" | "trn" | "cat";

const TITLES: Record<PageId, string> = { dash: "Dashboard", mon: "Monitoring", alrt: "Alerts", conn: "Connections", pip: "Pipelines", sch: "Schemas", qual: "Quality", lin: "Lineage", trn: "Transforms", cat: "Templates" };

/** What the shell hands every page. Each page declares the subset it uses (Dashboard/Pipelines/Settings: `go`; the pages a search result can
 *  land on: `focus` / `clearFocus`, see searchTarget.ts). */
interface PageProps extends PageFocusProps { go?: (id: PageId) => void; openTarget?: (target: SearchTarget) => void }

const PAGES: Record<PageId, React.ComponentType<PageProps>> = { dash: Dashboard, mon: Monitoring, alrt: Alerts, conn: Connections, pip: Pipelines, sch: Schemas, qual: Quality, lin: Lineage, trn: Transforms, cat: Templates };

// The permission that shows each page (per-button gating inside the page still applies). Every page needs one:
// nothing is visible to a user who holds no DataConnect permission.
const PAGE_PERMISSION: Record<PageId, Permission> = {
  dash: "dataconnect:dashboard:view",
  mon: "dataconnect:monitoring:view",
  alrt: "dataconnect:alerts:view",
  conn: "dataconnect:connections:view",
  pip: "dataconnect:pipelines:view",
  sch: "dataconnect:schemas:view",
  qual: "dataconnect:quality:view",
  lin: "dataconnect:lineage:view",
  trn: "dataconnect:transforms:view",
  cat: "dataconnect:templates:view",
};

// Which page was active survives a reload — localStorage, same as the
// dev-mode backend config (dc_base_url), so this follows the same convention
// rather than introducing a different one. Read once, at
// mount (a lazy useState initializer below), never subscribed to — two
// independent federated mounts on one host page (see tests/e2e/
// federation-host-contract.spec.ts's "two independent mounts" case) each
// read it once at their own mount time and never re-read it on the other
// mount's navigation, so they still can't see each other's state live; a
// page reload discards every mount and recreates them all anyway, so
// there's no "which mount wins" conflict to resolve.
const LAST_PAGE_STORAGE_KEY = "dc_last_page";

function isPageId(value: string | null): value is PageId {
  return !!value && value in PAGES;
}

function readPersistedPage(): PageId {
  try {
    const stored = typeof localStorage !== "undefined" ? localStorage.getItem(LAST_PAGE_STORAGE_KEY) : null;
    return isPageId(stored) ? stored : "dash";
  } catch {
    return "dash"; // private-browsing / storage-disabled — fall back rather than throw
  }
}

function persistPage(id: PageId): void {
  try { if (typeof localStorage !== "undefined") localStorage.setItem(LAST_PAGE_STORAGE_KEY, id); } catch { /* same fallback as above */ }
}

const NAV: { grp: string; items: PageId[] }[] = [
  { grp: "Overview", items: ["dash", "mon", "alrt"] },
  { grp: "Ingestion", items: ["conn", "pip"] },
  { grp: "Data Quality", items: ["sch", "qual", "lin", "trn"] },
  { grp: "Config", items: ["cat"] },  // Settings page removed in Developer Edition
];

function pageVisible(id: PageId, can: Can): boolean {
  return can(PAGE_PERMISSION[id]);
}

function NoAccess({ title, body, retry }: { title: string; body: string; retry?: () => void }) {
  return (
    <div className="alert alert-w" role="alert" data-testid="dc-access-denied" style={{ margin: "13px 13px 0" }}>
      <div className="at">{title}</div>
      <div className="ab">{body}</div>
      {retry && <button type="button" className="btn-s" style={{ marginTop: 8 }} onClick={retry}>Try again</button>}
    </div>
  );
}

function Logo() {
  return (
    <div className="sb-logo">
      <PralaiLockup height={34} label="DataConnect" />
    </div>
  );
}

interface ShellProps { embedded?: boolean; onError?: (error: Error) => void; accountMenu?: React.ReactNode; roleBadges?: React.ReactNode }

function Shell({ embedded, onError, accountMenu, roleBadges }: ShellProps) {
  // Hosted by the portal: its rail draws the bell and the profile, so the top bar carries neither.
  const hosted = roleBadges !== undefined;
  // Inside the Developer Edition dashboard: its left rail carries notifications and the account menu.
  const devShell = inDeveloperShell();
  const { cfg, me, access, accessError, can, viewingAs, refetchMe } = useApp();
  const [page, setPage] = useState<PageId>(readPersistedPage);
  // A search result picked in the top bar: switch to its page and hand the page the item to land on.
  const [focus, setFocus] = useState<SearchTarget | null>(null);
  const clearFocus = useCallback(() => setFocus(null), []);

  const go = useCallback((id: PageId) => { setPage(id); persistPage(id); }, []);
  const openTarget = useCallback((target: SearchTarget) => { setFocus(target); setPage(target.page); persistPage(target.page); }, []);
  const visiblePages = NAV.flatMap((group) => group.items).filter((id) => pageVisible(id, can));
  const searchPages = visiblePages.map((id) => ({ id, title: TITLES[id] }));
  const hasAccess = access === "ready" && can(ACCESS_PERMISSION) && visiblePages.length > 0;
  // The sidebar's Alerts entry carries the number of open alerts (sent to a channel, not yet acknowledged).
  const seesAlerts = access === "ready" && pageVisible("alrt", can);
  const { data: alertCounts } = useGetAlertCountsQuery(undefined, { skip: !seesAlerts, pollingInterval: ALERT_COUNTS_POLL_MS });
  const openAlerts = seesAlerts ? alertCounts?.open ?? 0 : 0;
  // Clicking Alerts in the sidebar opens its Open tab; a refresh keeps whichever tab was open (pages/Alerts.tsx reads this once).
  const fromSidebar = useCallback((id: PageId) => {
    if (id === "alrt") { try { sessionStorage.setItem(ALERTS_FROM_NAV_KEY, "1"); } catch { /* the remembered tab is used instead */ } }
    go(id);
  }, [go]);

  // A page the caller cannot see (a stored page from an earlier session, permissions that changed at the next sign-in, or a
  // "view as" role switched in the portal that does not include it) is replaced by the first one they can.
  useEffect(() => {
    if (access !== "ready") return;
    const first = visiblePages[0];
    if (!pageVisible(page, can) && first) go(first);
  }, [access, page, can, go, visiblePages]);

  const Page = PAGES[page] || Dashboard;
  const userName = me?.user || "";

  return (
    // The signed-in user and tenant (from GET /me) are not drawn in the sidebar; they stay readable here for tests/hosts (not visible UI).
    <div className={`app${embedded ? " embedded" : ""}`} data-dc-user={userName} data-dc-org={cfg.orgId} data-dc-access={access}>
      {!embedded && (
        <aside className="sb">
          <Logo />
          {NAV.map((group) => {
            const items = group.items.filter((id) => pageVisible(id, can));
            if (!items.length) return null;
            return (
              <React.Fragment key={group.grp}>
                <div className="sb-grp">{group.grp}</div>
                <div className="sb-nav">
                  {items.map((id) => (
                    // A real <button>, so Tab reaches it and Enter/Space activate it;
                    // aria-current tells a screen reader which page is open (the
                    // highlight alone is colour-only).
                    <button
                      type="button" key={id} className={`sn${page === id ? " active" : ""}`}
                      aria-current={page === id ? "page" : undefined} onClick={() => fromSidebar(id)}
                    >
                      <NavIcon id={id} /> {TITLES[id]}
                      {id === "alrt" && openAlerts > 0 && <span className="sn-count" aria-label={`${openAlerts} open alerts`}>{openAlerts}</span>}
                    </button>
                  ))}
                </div>
              </React.Fragment>
            );
          })}
        </aside>
      )}

      <div className="main">
        <div className="topbar">
          {/* The page's own heading (PH) names the page, so the bar carries no title of its own: the search on the left, who you are on
              the right. With no sidebar (embedded) the logo opens the bar instead, so it still sits right beside the portal's rail. */}
          {embedded && <div className="tb-logo"><Logo /></div>}
          <HeaderSearch pages={searchPages} canOpen={(id) => pageVisible(id, can)} onGo={go} onOpen={openTarget} />
          <div className="tb-r">
            {/* The portal's role strip (display only — never read; access comes from /me). One line: it scrolls sideways rather
                than wrapping the bar. */}
            {hosted && <div className="tb-roles" data-testid="dc-role-badges">{roleBadges}</div>}
            {/* An older portal passes its account menu for this place; a current one keeps the profile in its rail. Standalone, the
                user's own avatar. */}
            {/* Notifications: open alerts (sent to a channel, not yet acknowledged); opens the Alerts page. */}
            {!hosted && !embedded && !devShell && seesAlerts && (
              <button
                type="button" onClick={() => fromSidebar("alrt")} title="Notifications"
                aria-label={openAlerts > 0 ? `Notifications: ${openAlerts} open alerts` : "Notifications"}
                style={{ position: "relative", border: 0, background: "transparent", padding: 8, color: "var(--dc-icon-muted, #73827a)", cursor: "pointer", display: "grid", marginRight: 4 }}
              >
                <svg width={20} height={20} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" /><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
                </svg>
                {openAlerts > 0 && (
                  <span style={{ position: "absolute", top: 2, right: 2, minWidth: 16, height: 16, padding: "0 4px", borderRadius: 999, background: "#ef4444", color: "#fff", fontSize: 10, fontWeight: 700, lineHeight: "16px", textAlign: "center" }}>
                    {openAlerts > 99 ? "99+" : openAlerts}
                  </span>
                )}
              </button>
            )}
            {accountMenu ?? (!hosted && !embedded && !devShell && <DeveloperUserMenu name={userName} />)}
          </div>
        </div>
        {access === "loading" && <div className="ab" data-testid="dc-access-loading" style={{ margin: "13px" }}>Checking your access…</div>}
        {access === "error" && (
          <NoAccess title="Could not check your access" body={`DataConnect could not load your permissions: ${accessError ?? "unknown error"}`} retry={refetchMe} />
        )}
        {access === "ready" && !hasAccess && (viewingAs ? (
          <NoAccess title="Nothing to show for this role" body="The role you are viewing DataConnect as has no DataConnect pages. Choose another of your roles, or all roles, in the role strip above." />
        ) : (
          <NoAccess title="No access to DataConnect" body="Your account has no DataConnect permissions. Ask your organization admin for a role that includes DataConnect." />
        ))}
        {hasAccess && (
          <div className="cont">
            {/* FE-BUG-22: a render error on one screen shows a contained message (and
                clears when the user navigates) instead of blanking DataConnect inside the host. */}
            <ErrorBoundary label="This page" resetKey={page} onError={onError}>
              <Page go={go} focus={focus} clearFocus={clearFocus} openTarget={openTarget} />
            </ErrorBoundary>
          </div>
        )}
      </div>
    </div>
  );
}

export interface DataConnectAppProps {
  /** Same-origin tunnel base to the DataConnect FastAPI backend. Required when mounted by a host (FEDERATED): without it DataConnect
   * shows a configuration error and makes no requests, rather than falling back to a development backend. Optional only in a
   * standalone dev build (`npm run dev`). */
  apiBase?: string;
  /** Hide DataConnect's own sidebar when the host supplies chrome. */
  embedded?: boolean;
  /** Write telemetry, surfaced in the host activity bell. */
  onOperation?: (op: ApiOperation) => void;
  /** Called when a screen throws while rendering (FE-BUG-22). DataConnect shows
   * a contained fallback either way; this lets the host log or alert on it. */
  onError?: (error: Error) => void;
  /** The portal's display-only strip of DataConnect role names (the user's own highlighted). Rendered at the right of the top bar,
   * on one line. DataConnect never inspects it — what the user may do still comes from GET /me. Passing it (even `null`) means
   * "hosted by the portal": the portal's rail then carries the notification bell and the profile, so DataConnect draws no avatar
   * of its own. Leave it undefined for standalone use. */
  roleBadges?: React.ReactNode;
  /** @deprecated Older portals only — a current portal keeps the profile in its rail and passes `roleBadges` instead. The host's
   * account menu (profile, user management, sign out), rendered at the right of the top bar in place of DataConnect's own avatar. */
  accountMenu?: React.ReactNode;
  /** "View as role": the permission IDs of the one role the user chose in the portal's role strip. `null` / `undefined` = all their
   * roles (no narrowing; what older portals send). DataConnect then shows pages, tabs and buttons from GET /me's permissions ∩ these —
   * never wider than /me. Switching it re-renders in place (no remount); an open page that is no longer allowed moves to the first one
   * that is. UX only: never sent to the backend, which still authorises on the ticket. Permission IDs only, never role names. */
  viewPermissions?: readonly string[] | null;
}

export default function DataConnectApp({ apiBase, embedded = false, onOperation, onError, accountMenu, roleBadges, viewPermissions }: DataConnectAppProps) {
  return (
    <UiProvider>
      <div className="dc-root">
        {/* Outermost net: anything that breaks above the page level (providers,
            shell chrome) still leaves a message, not an empty box in the host. */}
        <ErrorBoundary label="DataConnect" onError={onError}>
          <AppProvider apiBase={apiBase} onOperation={onOperation} onError={onError} viewPermissions={viewPermissions}>
            <Shell embedded={embedded} onError={onError} accountMenu={accountMenu} roleBadges={roleBadges} />
            <Modal />
            <Toasts />
            <FastTooltip />
          </AppProvider>
        </ErrorBoundary>
      </div>
    </UiProvider>
  );
}
