import { useEffect, useState, type ReactNode } from "react";
import { BrowserRouter, Routes, Route, NavLink, useLocation, useNavigate } from "react-router-dom";
import { api } from "./api/client";
import { resolveTopbarSearch } from "./topbarSearch";
import { SEARCH_CLEARED } from "./searchEvents";
import DashboardPage from "./pages/DashboardPage";
import ModelCatalogPage from "./pages/ModelCatalogPage";
import BaseModelDetailPage from "./pages/BaseModelDetailPage";
import EndpointsPage from "./pages/EndpointsPage";
import AlertsPage from "./pages/AlertsPage";
import TrainPage from "./pages/TrainPage";
import PipelinesPage from "./pages/PipelinesPage";
import PipelineRunPage from "./pages/PipelineRunPage";
import TrainingRunDetailPage from "./pages/TrainingRunDetailPage";
import DeployPage from "./pages/DeployPage";
import ModelOverviewPage from "./pages/ModelOverviewPage";
import DatasetsPage from "./pages/DatasetsPage";
import ExperimentsPage from "./pages/ExperimentsPage";
import { PermissionsProvider, usePermissions } from "./context/PermissionsContext";
import { PAGE_PERMISSION, PERM } from "./permissions";
import { PralaiLockup } from "./components/PralaiLogo";
import NotificationsBell from "./components/NotificationsBell";
import { DeveloperUserMenu, inDeveloperShell } from "./components/DeveloperUserMenu";
import { ToastProvider } from "./context/ToastContext";
import { Icon } from "./components/Icons";
import { PageSearchProvider, usePageSearchState } from "./context/PageSearchContext";
import { hostAccountMenu } from "./runtimeConfig";

type NavItem = { to: string; label: string; icon: string; title: string; sub: string; badge?: string };
type NavGroup = { section: string; items: NavItem[] };
const NAV_GROUPS: NavGroup[] = [
  { section: "Overview", items: [
    { to: "/", label: "Dashboard", icon: "activity", title: "Dashboard", sub: "acme-corp's data plane — what's running, what's training" },
    { to: "/model-overview", label: "Model Overview", icon: "activity", title: "Model Overview", sub: "Full observability per model — performance, drift, predictions, explainability, infra" },
  ] },
  { section: "Build", items: [
    { to: "/catalog", label: "Model catalog", icon: "grid", title: "Model catalog", sub: "Every model — in one place" },
    { to: "/train", label: "Train wizard", icon: "wand", title: "Train wizard", sub: "Base model → dataset → compute → launch" },
  ] },
  { section: "Data", items: [
    { to: "/datasets", label: "Datasets", icon: "database", title: "Datasets", sub: "Uploads and Iceberg tables, both registered in Catalog" },
  ] },
  { section: "Operate", items: [
    { to: "/pipelines", label: "Pipelines", icon: "branch", title: "Pipelines", sub: "One Argo Workflow per training request" },
    { to: "/endpoints", label: "Endpoints", icon: "plug", title: "Endpoints", sub: "KServe and vLLM, both scaled by KEDA" },
    { to: "/experiments", label: "Experiments", icon: "flask", title: "Experiments", sub: "Native tracking — Postgres + object storage, no MLflow" },
    { to: "/deploy", label: "Deploy & promote", icon: "box", title: "Deploy & promote", sub: "Alias-based promotion — a metadata move, not a rewritten artifact" },
    { to: "/alerts", label: "Alerts", icon: "bell", title: "Alerts", sub: "Threshold policies, 3-window debounce, and Slack/Teams delivery" },
  ] },
];
const NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((g) => g.items);

/** Standalone only: who is signed in (from /api/me). Inside the portal its account menu takes this place. */
function UserBadge() {
  const { me } = usePermissions();
  return <DeveloperUserMenu name={me?.user || ""} />;
}

// Drill-down routes that have no nav entry of their own but still need a real
// title in the topbar — without this they'd fall through to the generic
// "ML Studio" and the header would say nothing about where you are.
const DETAIL_ROUTE_META: { prefix: string; title: string; sub: string }[] = [
  { prefix: "/base-models/", title: "Base model", sub: "Library entry — details, deployment profile, and what's derived from it" },
  { prefix: "/training-runs/", title: "Training run", sub: "One run end to end — stages, what it measured, the acceptance gate, and how to reproduce it" },
  { prefix: "/pipelines/", title: "Pipeline run", sub: "Stages and the run's own recorded log" },
];

function Topbar({ roleBadges }: { roleBadges?: ReactNode }) {
  const { pathname } = useLocation();
  // The portal passes roleBadges when it hosts ML Studio; its rail then carries
  // the bell and the user, so the bar drops its own.
  const hosted = roleBadges !== undefined;
  const navigate = useNavigate();
  // Shared with the page on screen: a page with lists filters as you type; on a
  // page without one, Enter searches models, base models and datasets globally.
  const { query: q, setQuery: setQ, placeholder } = usePageSearchState();
  // A page's Clear empties this box too.
  useEffect(() => {
    const clear = () => setQ("");
    window.addEventListener(SEARCH_CLEARED, clear);
    return () => window.removeEventListener(SEARCH_CLEARED, clear);
  }, [setQ]);
  // Resolved against live lists at the moment of searching, so it works from any page.
  const search = async () => {
    if (!q.trim() || placeholder) return;
    const [models, base, datasets] = await Promise.all([
      api.getModels().catch(() => []), api.getBaseModels().catch(() => []), api.getDatasets().catch(() => []),
    ]);
    navigate(resolveTopbarSearch(q, models, base, datasets));
  };
  const detail = DETAIL_ROUTE_META.find((d) => pathname.startsWith(d.prefix));
  const navActive = NAV_ITEMS.slice().reverse().find((n) => (n.to === "/" ? pathname === "/" : pathname.startsWith(n.to)));
  const active = detail ?? navActive;
  // Each page shows its own heading, so the bar carries search and user
  // (same order as the catalog's header); the page name lives on in the tab title.
  useEffect(() => {
    document.title = active ? `${active.title} · ML Studio` : "ML Studio";
  }, [active]);
  return (
    <header className="topbar">
      <div className="search-box">
        <Icon name="search" />
        <input type="text" placeholder={placeholder ?? "Search models or datasets…"} aria-label={placeholder ?? "Search models or datasets"}
          value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") search(); if (e.key === "Escape") setQ(""); }} />
        {q && (
          <button type="button" className="search-clear has-tip" aria-label="Clear search" data-tip="Clear search (Esc)" onClick={() => setQ("")}>
            <Icon name="x" />
          </button>
        )}
      </div>
      <div className="topbar-spacer" />
      {/* Display only: the portal's strip of ML Studio role names. Access still comes from /api/me. */}
      {hosted && <div className="topbar-roles">{roleBadges}</div>}
      {/* Inside the Developer Edition dashboard its left rail carries notifications and the account menu. */}
      {!hosted && !inDeveloperShell() && <NotificationsBell />}
      {/* An older portal passes its account menu instead of roleBadges; it takes the avatar's place. */}
      {hostAccountMenu() ?? (hosted || inDeveloperShell() ? null : <UserBadge />)}
    </header>
  );
}

function Sidebar() {
  const { can } = usePermissions();
  const { pathname } = useLocation();
  const seesPromotions = can(PERM.deploy) || can(PERM.dashboard);
  // Live count of versions awaiting promotion — re-read on every navigation and
  // every 30s, so promoting a model (or a run finishing) updates the badge.
  const [pending, setPending] = useState<number | null>(null);
  useEffect(() => {
    if (!seesPromotions) { setPending(null); return; }
    let alive = true;
    const read = () => api.getPendingPromotions()
      .then((list) => { if (alive) setPending(list.length); })
      .catch(() => { if (alive) setPending(null); });
    read();
    const timer = setInterval(read, 30000);
    return () => { alive = false; clearInterval(timer); };
  }, [pathname, seesPromotions]);
  const badgeFor = (item: NavItem) => item.to === "/deploy" ? (pending ? String(pending) : undefined) : item.badge;
  return (
    <aside className="sidebar">
      <div className="brand">
        <PralaiLockup height={34} label="ML Studio" />
      </div>

      {NAV_GROUPS.map((group) => {
        // A page is listed only when its permission is held (unknown/loading = hidden).
        const visible = group.items.filter((item) => can(PAGE_PERMISSION[item.to]));
        // A section with nothing left in it shouldn't leave a dangling heading.
        if (visible.length === 0) return null;
        return (
        <div key={group.section}>
          <div className="nav-section-label">{group.section}</div>
          <ul className="nav">
            {visible.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.to === "/"}
                  className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}
                >
                  <Icon name={item.icon} />
                  {item.label}
                  {badgeFor(item) && <span className="nav-badge">{badgeFor(item)}</span>}
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
        );
      })}

      <div className="sidebar-spacer" />

      <div className="sidebar-version">v2.37.1</div>
    </aside>
  );
}

/**
 * Shows a page only when the user's permissions (from /api/me) allow it.
 *
 * While permissions load nothing is shown; a deep link or a browser Back into a
 * page the user can't open lands on the first page they can (or a notice when
 * there is none), rather than a screen with no way out of it. Actions inside
 * pages check their own permission with usePermissions().can. Presentation
 * only — the backend checks every request independently.
 */
function PermissionGuard({ children }: { children: ReactNode }) {
  const { status, canReach, error } = usePermissions();
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const firstAllowed = NAV_ITEMS.find((n) => canReach(n.to))?.to ?? null;
  const allowed = status === "ready" && canReach(pathname);

  useEffect(() => {
    if (status === "ready" && !allowed && firstAllowed && firstAllowed !== pathname) navigate(firstAllowed, { replace: true });
  }, [status, allowed, firstAllowed, pathname, navigate]);

  if (status === "loading") return <div className="empty-hint" role="status">Loading your access…</div>;
  if (status === "error") {
    return <div className="callout callout-amber" role="status">ML Studio could not read your access ({error}). Nothing is shown until it can.</div>;
  }
  if (!firstAllowed) {
    return <div className="callout callout-amber" role="status">Your account has no ML Studio permissions. Ask an administrator for access.</div>;
  }
  return allowed ? <>{children}</> : null;
}

/**
 * Everything inside the router: the chrome and the route table.
 *
 * Split out of App so the same shell can be mounted by two different routers —
 * BrowserRouter when ML Studio runs standalone (the URL is its own), and
 * MemoryRouter when the Control Portal federates it in, where the host owns the
 * address bar and a second BrowserRouter would fight it for the URL.
 *
 * `embedded` hides ML Studio's own sidebar and top bar, because the host
 * supplies that chrome and the signed-in identity — the same contract
 * DataConnect's `embedded` prop uses. `roleBadges` (from the portal) marks it
 * hosted: the strip sits at the right of the top bar in place of the bell and avatar.
 */
export function AppShell({ embedded = false, roleBadges }: { embedded?: boolean; roleBadges?: ReactNode }) {
  return (
    <PageSearchProvider>
    <div className={"app" + (embedded ? " embedded" : "")}>
      {!embedded && <Sidebar />}
      <div className="main">
        {!embedded && <Topbar roleBadges={roleBadges} />}
        <div id="views">
          <div className="view">
            <div className="view-body">
              <PermissionGuard>
                <Routes>
                  <Route path="/" element={<DashboardPage />} />
                  <Route path="/model-overview" element={<ModelOverviewPage />} />
                  <Route path="/datasets" element={<DatasetsPage />} />
                  <Route path="/experiments" element={<ExperimentsPage />} />
                  <Route path="/catalog" element={<ModelCatalogPage />} />
                  <Route path="/base-models/:id" element={<BaseModelDetailPage />} />
                  <Route path="/train" element={<TrainPage />} />
                  <Route path="/pipelines" element={<PipelinesPage />} />
                  <Route path="/pipelines/:id" element={<PipelineRunPage />} />
                  <Route path="/training-runs/:id" element={<TrainingRunDetailPage />} />
                  <Route path="/deploy" element={<DeployPage />} />
                  <Route path="/endpoints" element={<EndpointsPage />} />
                  <Route path="/alerts" element={<AlertsPage />} />
                </Routes>
              </PermissionGuard>
            </div>
          </div>
        </div>
      </div>
    </div>
    </PageSearchProvider>
  );
}

export default function App() {
  return (
    <PermissionsProvider>
      <ToastProvider>
        <BrowserRouter>
          <AppShell />
        </BrowserRouter>
      </ToastProvider>
    </PermissionsProvider>
  );
}
