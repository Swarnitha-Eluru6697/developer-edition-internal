// Permission IDs this UI checks, as declared in the backend's permissions.json
// (InaiEra_agent_backend). The UI never sees or decides on roles: which
// permissions a user holds is decided in the Admin Portal and arrives here
// through GET /api/me. Hiding a page or a button is convenience only — the
// API checks the same permission on every request.

export const P = {
  accessView: 'agent:access:view',
  overviewView: 'agent:overview:view',
  studioEdit: 'agent:studio:edit',
  templatesView: 'agent:templates:view',
  templatesManagePlatform: 'agent:templates:manage-platform',
  ragManage: 'agent:rag:manage',
  integrationsManage: 'agent:integrations:manage',
  runsView: 'agent:runs:view',
  tracesView: 'agent:traces:view',
  monitoringView: 'agent:monitoring:view',
  guardrailsManage: 'agent:guardrails:manage',
  alertsManage: 'agent:alerts:manage',
  webappsUse: 'agent:webapps:use',
  settingsEdit: 'agent:settings:edit',
  agentsRun: 'agent:agents:run',
  agentsPublish: 'agent:agents:publish',
  maintenanceRun: 'agent:maintenance:run',
} as const;

export type Permission = (typeof P)[keyof typeof P];

/** Every ID above, e.g. for tests and the dev-server identity stand-in. */
export const ALL_PERMISSIONS: Permission[] = Object.values(P);

/** Pages that read the agent list — the sidebar's agent-count badge and the
 *  top-bar search only fetch agents for a user holding one of these. */
export const AGENT_READER_PERMS: Permission[] = [
  P.overviewView, P.studioEdit, P.runsView, P.tracesView, P.monitoringView, P.guardrailsManage, P.alertsManage,
];

/** Pages that show the open-alert tally (getHomeStats). */
export const ALERT_COUNT_PERMS: Permission[] = [P.overviewView, P.monitoringView, P.alertsManage];

/** Pages that read knowledge bases (top-bar search). */
export const KB_READER_PERMS: Permission[] = [P.ragManage, P.studioEdit];

/** The standalone published-app runner (/app/:agentId). */
export const RUNNER_PERMS: Permission[] = [P.webappsUse, P.agentsRun, P.studioEdit];

/** Route path -> the permission that opens it. Pages without an entry of
 *  their own (/builder, /snippets) belong to Studio. */
export const ROUTE_PERMISSION: Record<string, Permission> = {
  '/': P.overviewView,
  '/agents': P.studioEdit,
  '/builder': P.studioEdit,
  '/snippets': P.studioEdit,
  '/templates': P.templatesView,
  '/rag': P.ragManage,
  '/integrations': P.integrationsManage,
  '/runs': P.runsView,
  '/traces': P.tracesView,
  '/monitoring': P.monitoringView,
  '/guardrails': P.guardrailsManage,
  '/alerts': P.alertsManage,
  '/webapps': P.webappsUse,
  '/settings': P.settingsEdit,
};
