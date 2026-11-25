import React, { useEffect, useRef, useState } from 'react';
import { HashRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { AppProvider, useApp } from './context/AppContext';
import { api } from './api';
import { NAV, navPath } from './constants';
import { AGENT_READER_PERMS, ALERT_COUNT_PERMS, ROUTE_PERMISSION, RUNNER_PERMS } from './permissions';
import IconSprite from './components/IconSprite';
import Sidebar from './components/Sidebar';
import Topbar from './components/Topbar';
import DrawerHost from './components/DrawerHost';
import ModalHost from './components/ModalHost';
import ToastHost from './components/ToastHost';
import { PralaiLockup } from './components/PralaiLogo';
import Home from './pages/Home';
import Agents from './pages/Agents';
import Snippets from './pages/Snippets';
import Builder from './pages/Builder';
import Templates from './pages/Templates';
import RAG from './pages/RAG';
import Integrations from './pages/Integrations';
import Runs from './pages/Runs';
import Traces from './pages/Traces';
import Monitoring from './pages/Monitoring';
import Alerts from './pages/Alerts';
import Settings from './pages/Settings';
import Guardrails from './pages/Guardrails';
import WebApps from './pages/WebApps';
import AgentRunner from './pages/AgentRunner';
import NodeDocs from './pages/NodeDocs';

/** Shown when the user holds none of the permissions that open a page. */
export function NoAccess() {
  return (
    <div className="wrap" role="status" style={{ padding: '48px 24px' }}>
      <h2 style={{ fontFamily: 'var(--disp)', fontSize: 18, margin: '0 0 8px' }}>You don't have access to Agents</h2>
      <p className="muted small" style={{ margin: 0 }}>
        Ask your administrator to grant you a permission for this application.
      </p>
    </div>
  );
}

/** The page a user lands on when the one they asked for is not theirs: the
 *  first sidebar entry they hold, or null when they hold none. */
function useFirstPermittedPath(): string | null {
  const { can } = useApp();
  for (const g of NAV) for (const it of g.items) if (can(it.permission)) return navPath(it.id);
  return null;
}

/** Renders a page only for a user holding its permission. While /api/me is
 *  still loading nothing is rendered; once it has answered, a page the user
 *  may not open redirects to their first permitted page. */
function Guarded({ path, children }: { path: string; children: React.ReactNode }) {
  const { can, permissionsStatus } = useApp();
  const fallback = useFirstPermittedPath();
  if (permissionsStatus === 'loading') return null;
  if (can(ROUTE_PERMISSION[path])) return <>{children}</>;
  if (fallback && fallback !== path) return <Navigate to={fallback} replace />;
  return <NoAccess />;
}

/** The standalone runner for a published app (/app/:agentId). */
function GuardedRunner() {
  const { canAny, permissionsStatus } = useApp();
  if (permissionsStatus === 'loading') return null;
  if (!canAny(RUNNER_PERMS)) return <NoAccess />;
  return <><AgentRunner /><ToastHost /></>;
}

function Shell() {
  const [agentCount, setAgentCount] = useState(0);
  const [openAlertCount, setOpenAlertCount] = useState(0);
  const { canAny, dataVersion, permissionsStatus } = useApp();
  const readsAgents = canAny(AGENT_READER_PERMS);
  const readsAlertCount = canAny(ALERT_COUNT_PERMS);
  const fallback = useFirstPermittedPath();
  const location = useLocation();
  const mainRef = useRef<HTMLDivElement>(null);

  // Publishes the pinned page header's real height as --ph-h on .main, so a
  // page's filter bar / tab strip can stick directly beneath it (see the
  // .wrap > .studio-filterbar rule in styles.css). The height genuinely
  // varies — an eyebrow, a subtitle that wraps at narrow widths, taller
  // action buttons — so it is measured rather than hardcoded, and observed
  // for resize instead of read once.
  useEffect(() => {
    const main = mainRef.current;
    if (!main) return;
    const head = main.querySelector('.pagehead');
    const sync = () => main.style.setProperty('--ph-h', `${(head as HTMLElement | null)?.offsetHeight ?? 0}px`);
    sync();
    if (!head || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(sync);
    ro.observe(head);
    return () => ro.disconnect();
  }, [location.pathname]);

  // dataVersion, not [] — fetched once, the alert badge stayed stale
  // after an alert was resolved, so the sidebar kept claiming work that
  // was already done.
  // Each badge is fetched only for a user who holds a page that reads it —
  // the API would refuse the call otherwise.
  useEffect(() => {
    if (readsAgents) {
      api.listAgents().then((a) => setAgentCount(a.filter((x) => x.plane !== 'Snippet').length)).catch(() => {});
    }
    // The server's tally, not the length of a list: listAlerts is paginated, so its
    // length stops at one page, and the Overview card reads this same field — reading
    // it here is what keeps the two numbers identical.
    if (readsAlertCount) {
      api.getHomeStats().then((s) => setOpenAlertCount(s.openAlerts)).catch(() => {});
    }
    // pathname too: alerts arrive in the background, and a page you navigate to fetches
    // its numbers fresh — the badge has to refresh at the same moment to match them.
    // readsAgents/readsAlertCount: /api/me answers after the first render.
  }, [dataVersion, location.pathname, readsAgents, readsAlertCount]);

  return (
    <div className="app">
      <div className="brandbar">
        <PralaiLockup height={36} label="Agent Studio" />
      </div>
      <Topbar />
      <Sidebar agentCount={agentCount} openAlertCount={openAlertCount} />
      <div className="main" ref={mainRef}>
        <Routes>
          <Route path="/" element={<Guarded path="/"><Home /></Guarded>} />
          <Route path="/agents" element={<Guarded path="/agents"><Agents /></Guarded>} />
          <Route path="/snippets" element={<Guarded path="/snippets"><Snippets /></Guarded>} />
          <Route path="/builder" element={<Guarded path="/builder"><Builder /></Guarded>} />
          <Route path="/templates" element={<Guarded path="/templates"><Templates /></Guarded>} />
          <Route path="/rag" element={<Guarded path="/rag"><RAG /></Guarded>} />
          <Route path="/integrations" element={<Guarded path="/integrations"><Integrations /></Guarded>} />
          <Route path="/runs" element={<Guarded path="/runs"><Runs /></Guarded>} />
          <Route path="/traces" element={<Guarded path="/traces"><Traces /></Guarded>} />
          <Route path="/monitoring" element={<Guarded path="/monitoring"><Monitoring /></Guarded>} />
          <Route path="/alerts" element={<Guarded path="/alerts"><Alerts /></Guarded>} />
          <Route path="/settings" element={<Guarded path="/settings"><Settings /></Guarded>} />
          <Route path="/guardrails" element={<Guarded path="/guardrails"><Guardrails /></Guarded>} />
          <Route path="/webapps" element={<Guarded path="/webapps"><WebApps /></Guarded>} />
          {/* Anything else: the user's first permitted page, or the "no access" note. */}
          <Route path="*" element={permissionsStatus === 'loading' ? null
            : fallback ? <Navigate to={fallback} replace /> : <NoAccess />} />
        </Routes>
      </div>
      <DrawerHost />
      <ModalHost />
      <ToastHost />
    </div>
  );
}

/** The app's routes without a router, so the standalone build can use a
 *  HashRouter and the federated build (AgenticWorkflowApp) a MemoryRouter
 *  that never touches the host portal's URL. */
export function AppRoutes({ viewPermissions }: { viewPermissions?: string[] | null } = {}) {
  return (
    <AppProvider viewPermissions={viewPermissions}>
      <IconSprite />
      <Routes>
        {/* A standalone layout — no Studio sidebar/topbar. This is what
            someone consuming a published app sees, not the authoring
            shell every other route uses. */}
        <Route path="/app/:agentId" element={<GuardedRunner />} />
        <Route path="/docs/:nodeType" element={<NodeDocs />} />
        <Route path="/*" element={<Shell />} />
      </Routes>
    </AppProvider>
  );
}

export default function App() {
  return (
    <HashRouter>
      <AppRoutes />
    </HashRouter>
  );
}
