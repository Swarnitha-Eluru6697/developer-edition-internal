import type {
  AgentRoleDef, MlStudioDeployment, LlmModelOption,
  GitConnection, GitProvider, GitRepo, GitCommitResult,
  ChatHistoryEntry,
  Agent, AgentLog, Run, Trace, TraceSpansResponse, Alert, AlertDestination, AlertDestinationIn,
  AlertRule, AlertRuleIn, AlertRuleKind, AlertRulePreview, AlertSweepResult, Role,
  AgentLineage, AgentLiveJob, AgentQuality, CatalogPublishResult,
  ModelProvider, Tool, ToolAction, MCPServer, DataSource, DataSourceBrowseResult, SourceIngestResult,
  Template, TemplateVersion, SaveTemplateResult, PublishTemplateResult, Eval, HomeStats, EnvVar, KnowledgeBase,
  EvalSet, EvalCase, EvalResultRow, EvalRunRow, AgentFeatures, SavedResult, AgentVersion, EvalHealth, KBIngestResult,
  KnowledgeDocument, KnowledgeChunk, KnowledgeMetadataField, KnowledgeRetrievalTestResult, KnowledgeRetrievalTestHistoryEntry,
  BatchRunResult, KBSettings, KBRetrieveResult, AgentInputVariable, RuntimeConfig, GuardrailsOverview,
  Me,
  StorageTarget, StorageDestination, AgentSkillRef, AgentFileRef, AgentAssetLimits, AgentToolEntry, NodeSecretKey, NodeEnvResult, BindRequirementResult, TemplatePolicy,
} from './types';

// Real, deploy-without-code-changes base URL (§0.44, GAP_REPORT) — was
// hardcoded to http://127.0.0.1:8000 with no override at all, which
// would have silently broken every real API call the moment this app
// was deployed anywhere but localhost (unlike every backend-side
// self-referential URL, which already used a real, overridable env
// var). window.location.origin is correct standalone behind a
// same-origin gateway routing /api, /mcp and /embed.js to the backend;
// federated into the Control Portal, setApiBase() points it at the
// host's tunnel route instead. In local dev, vite.config.ts's dev-server
// proxy forwards those same paths to the backend running separately.
export let BASE = window.location.origin;

/** Point every API call at the host's tunnel route when this app runs as a
 *  Module-Federation remote inside the Control Portal (see AgenticWorkflowApp).
 *  `apiBase` is same-origin (e.g. /control_plane/api/dataplane/agenticworkflow-api),
 *  so the portal's session cookie rides along and Envoy injects the identity. */
export function setApiBase(apiBase: string): void {
  BASE = window.location.origin + apiBase.replace(/\/+$/, '');
}

export class ApiError extends Error {
  status: number;
  /** The raw `detail` as the server sent it, so a caller that wants the
   *  structure (the Builder reads `issues` to highlight nodes) is not
   *  forced to parse it back out of the message. */
  detail: unknown;
  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

/** Turn a FastAPI `detail` into something a person can read.
 *
 *  This used to be passed straight to `Error`, whose message is a
 *  string — so every endpoint that answers with a STRUCTURED detail
 *  surfaced in the UI as the literal text "[object Object]". Publish is
 *  the one people hit: flow validation answers with
 *  `{message, issues:[{message, node_id}]}`, so a blocked publish said
 *  nothing at all about what was wrong with the flow. FastAPI's own
 *  request-validation errors are a LIST of objects and had the same
 *  problem.
 */
export function formatApiDetail(detail: unknown, fallback: string): string {
  if (detail === null || detail === undefined) return fallback;
  if (typeof detail === 'string') return detail;

  // Flow validation and anything else shaped { message, issues[] }.
  if (typeof detail === 'object' && !Array.isArray(detail)) {
    const obj = detail as Record<string, any>;
    const head = typeof obj.message === 'string' ? obj.message : '';
    const issues = Array.isArray(obj.issues) ? obj.issues : [];
    if (head || issues.length) {
      const lines = issues.map((i: any) => {
        const where = i?.node_id ? ` (node ${i.node_id})` : '';
        return `• ${i?.message ?? JSON.stringify(i)}${where}`;
      });
      return [head, ...lines].filter(Boolean).join('\n');
    }
  }

  // FastAPI request-validation errors: [{ loc: [...], msg: "..." }].
  if (Array.isArray(detail)) {
    const lines = detail.map((d: any) => {
      if (typeof d === 'string') return `• ${d}`;
      const field = Array.isArray(d?.loc) ? d.loc.filter((p: any) => p !== 'body').join('.') : '';
      return `• ${field ? `${field}: ` : ''}${d?.msg ?? JSON.stringify(d)}`;
    });
    return lines.join('\n') || fallback;
  }

  // Anything else: show the JSON rather than "[object Object]".
  try {
    return JSON.stringify(detail);
  } catch {
    return fallback;
  }
}

export interface Page<T> {
  items: T[];
  /** Pass back as `cursor` to get the next page; null when there is none. */
  nextCursor: string | null;
  hasMore: boolean;
}

/** Like req(), but keeps the keyset-pagination headers the body drops.
 *
 * The server answers with X-Next-Cursor and X-Has-More; req() returns
 * only the parsed body, so a caller using it could never page at all —
 * it would silently show the first page and nothing else. */
/** The message for a failed response, whatever shape its body has.
 *
 *  An unexpected server error answers `{error_code, correlation_id}` with no
 *  `detail`, and the request-size check used to answer in plain text — both
 *  surfaced as a bare "Request failed: 500/413". The correlation id is what
 *  finds the full error in the server log, so it is shown, not dropped. */
function errorMessageFor(body: any, status: number): string {
  if (body && body.detail != null) return formatApiDetail(body.detail, `Request failed: ${status}`);
  if (body && body.correlation_id) {
    return `Something went wrong on the server, so this could not be completed. It has been logged — reference ${body.correlation_id}.`;
  }
  if (status === 413) return 'This file is too large to upload.';
  return `Request failed: ${status}`;
}

let uploadLimitPromise: Promise<number | null> | null = null;
/** Refuse a file over the server's upload cap before sending it, naming the
 *  limit — rather than uploading it all and then getting a 413. The cap is
 *  read once from the server, so MAX_UPLOAD_MB there is the one setting. */
async function checkUploadSize(file: File): Promise<void> {
  if (!uploadLimitPromise) {
    uploadLimitPromise = fetch(`${BASE}/api/uploads/limits`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => (d && typeof d.max_upload_bytes === 'number' ? d.max_upload_bytes : null))
      .catch(() => null);
  }
  const max = await uploadLimitPromise;
  if (max != null && file.size > max) {
    const mb = (n: number) => `${(n / (1024 * 1024)).toFixed(n >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
    throw new ApiError(`"${file.name}" is ${mb(file.size)}, which is too large to upload. Files must be ${mb(max)} or smaller.`, 413);
  }
}

async function reqPaged<T>(path: string): Promise<Page<T>> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
  }).catch((e) => {
    const cause = e instanceof Error ? e.message : 'network error';
    throw new Error(`Couldn't reach the backend at ${BASE} — is it running? (${cause})`);
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(
      errorMessageFor(body, res.status), res.status, body.detail);
  }
  return {
    items: (await res.json()) as T[],
    nextCursor: res.headers.get('X-Next-Cursor'),
    hasMore: res.headers.get('X-Has-More') === 'true',
  };
}

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    const isFormData = opts.body instanceof FormData;
    // Headers are MERGED, not replaced. `...opts` used to come after a
    // `headers` default, so any caller passing a header of its own
    // dropped Content-Type: application/json with it — and every
    // optimistic-locking mutation passes one (If-Match). FastAPI then
    // refused to parse the JSON body at all:
    //
    //   POST /api/agents/{id}/nodes -> 422
    //   "Input should be a valid dictionary or object to extract fields from"
    //
    // which is why the Builder could not add a node, move a node, edit a
    // node's config or draw a connection, while deleting still worked —
    // a DELETE carries no body, so having no content type costs it
    // nothing. It looked like drag-and-drop was broken; the drag was
    // fine and the request it sent was unparseable.
    const { headers: callerHeaders, ...rest } = opts;
    res = await fetch(`${BASE}${path}`, {
      ...rest,
      headers: {
        ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
        ...(callerHeaders as Record<string, string> | undefined),
      },
    });
  } catch (e) {
    // A network-level failure means the request never reached any
    // server — the backend isn't running, it's on a different port, or
    // it's genuinely unreachable from here. Browsers collapse all of
    // these into one bare "Failed to fetch" / "Load failed" with no
    // detail, which is undiagnosable on its own — name what was
    // actually being reached instead.
    const cause = e instanceof Error ? e.message : 'network error';
    throw new Error(`Couldn't reach the backend at ${BASE} — is it running? (${cause})`);
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    // Real, honest status preservation (§0.71, GAP_REPORT) — a plain
    // Error lost the real HTTP status code, so a caller (e.g. the
    // Builder's own optimistic-locking conflict handling) couldn't
    // genuinely distinguish a real 409 Conflict from any other real
    // failure without fragile message-string matching.
    throw new ApiError(
      errorMessageFor(body, res.status), res.status, body.detail);
  }
  return res.json();
}

export interface UploadedFileRef {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  url: string;
}

export interface StreamEvent {
  type: 'node_start' | 'node_done' | 'complete' | 'error' | 'cancelled';
  node_id?: string;
  node_type?: string;
  node?: string;
  status?: string;
  [key: string]: any;
}

/** Real Server-Sent Events consumer for POST /run/stream — genuine live
 * node_start/node_done events as the backend's harness traversal actually
 * reaches each node (see routers/execution.py's run_agent_stream), not a
 * replay of an already-finished run animated to look live. Not built on
 * the shared req() helper above since it needs incremental chunk reads,
 * not a single parsed JSON body. */
async function streamAgentRun(
  agentId: string, input: string, invokingUser: string, inputs: Record<string, any> | undefined,
  onEvent: (evt: StreamEvent) => void, signal: AbortSignal, preview?: boolean,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${BASE}/api/agents/${agentId}/run/stream`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input, invoking_user: invokingUser, inputs, preview }),
      signal,
    });
  } catch (e) {
    if (signal.aborted) return; // real user-initiated stop, not an error to surface
    const cause = e instanceof Error ? e.message : 'network error';
    throw new Error(`Couldn't reach the backend at ${BASE} — is it running? (${cause})`);
  }
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body && body.detail != null
      ? formatApiDetail(body.detail, `Stream failed: ${res.status}`)
      : errorMessageFor(body, res.status));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (e) {
      if (signal.aborted) return;
      throw e;
    }
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    const parts = buffer.split('\n\n');
    buffer = parts.pop() || '';
    for (const part of parts) {
      const line = part.trim();
      if (line.startsWith('data: ')) {
        try { onEvent(JSON.parse(line.slice(6))); } catch { /* malformed chunk boundary — skip, next chunk recovers */ }
      }
    }
  }
}

export const api = {
  streamAgentRun,
  uploadFile: async (file: File) => {
    await checkUploadSize(file);
    const form = new FormData();
    form.append('file', file);
    return req<UploadedFileRef>('/api/uploads', { method: 'POST', body: form });
  },
  // Agent node: skills and reference files
  listAgentSkills: (agentId: string) => req<AgentSkillRef[]>(`/api/agents/${agentId}/skills`),
  uploadAgentSkill: (agentId: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return req<AgentSkillRef>(`/api/agents/${agentId}/skills`, { method: 'POST', body: form });
  },
  deleteAgentSkill: (agentId: string, id: string) => req<{ ok: boolean; outcome?: 'deleted' | 'retired' }>(`/api/agents/${agentId}/skills/${id}`, { method: 'DELETE' }),
  listAgentFiles: (agentId: string) => req<AgentFileRef[]>(`/api/agents/${agentId}/files`),
  uploadAgentFile: (agentId: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return req<AgentFileRef>(`/api/agents/${agentId}/files`, { method: 'POST', body: form });
  },
  deleteAgentFile: (agentId: string, id: string) => req<{ ok: boolean; outcome?: 'deleted' | 'retired' }>(`/api/agents/${agentId}/files/${id}`, { method: 'DELETE' }),
  reconcileAgentAssets: (agentId: string) => req<{ skills: number; files: number }>(`/api/agents/${agentId}/assets/reconcile`, { method: 'POST' }),
  // Agent node Env: values are write-only; only key names are ever read back.
  listNodeSecrets: (agentId: string, nodeId: string) => req<NodeSecretKey[]>(`/api/agents/${agentId}/nodes/${nodeId}/secrets`),
  // Env is set in ONE transactional call: the node's key list and the encrypted values commit together
  // (a stale revision is a 409 and nothing is written). `entries` is the full list of keys the node
  // declares; an entry with a value writes it, one without keeps the stored value.
  putNodeEnv: (agentId: string, nodeId: string, entries: { key: string; value?: string }[], ifMatch?: number) =>
    req<NodeEnvResult>(`/api/agents/${agentId}/nodes/${nodeId}/env`, {
      method: 'PUT', body: JSON.stringify({ entries }),
      ...(ifMatch != null ? { headers: { 'If-Match': String(ifMatch) } } : {}),
    }),
  bindFileRequirement: (agentId: string, nodeId: string, requirementId: string, fileId: string, ifMatch?: number) =>
    req<BindRequirementResult>(`/api/agents/${agentId}/nodes/${nodeId}/requirements/${encodeURIComponent(requirementId)}/bind`, {
      method: 'POST', body: JSON.stringify({ file_id: fileId }),
      ...(ifMatch != null ? { headers: { 'If-Match': String(ifMatch) } } : {}),
    }),
  setFileTemplatePolicy: (agentId: string, fileId: string, template_policy: TemplatePolicy) =>
    req<AgentFileRef>(`/api/agents/${agentId}/files/${fileId}`, { method: 'PATCH', body: JSON.stringify({ template_policy }) }),
  getAgentToolCatalog: () => req<{ tools: AgentToolEntry[] }>('/api/integrations/agent-tool-catalog'),
  getAgentAssetLimits: (agentId: string) => req<AgentAssetLimits>(`/api/agents/${agentId}/files/limits`),
  // agents
  // Computed by the platform from its own runs and evals, and published
  // to UCS from there. Reading them back from the catalogue would show
  // only what UCS had already been told.
  getAgentLineage: (id: string) => req<AgentLineage>(`/api/agents/${id}/lineage`),
  getAgentQuality: (id: string) => req<AgentQuality>(`/api/agents/${id}/quality`),
  getAgentLiveJobs: (id: string) => req<{ jobs: AgentLiveJob[] }>(`/api/agents/${id}/live-jobs`),
  publishAgentCatalog: (id: string) =>
    req<CatalogPublishResult>(`/api/agents/${id}/catalog/publish`, { method: 'POST' }),

  listAgents: (plane?: 'Data' | 'App' | 'Snippet') => req<Agent[]>(`/api/agents${plane ? `?plane=${plane}` : ''}`),
  /** Every agent, not the first page of them.
   *
   * `listAgents` hits a paginated endpoint that returns at most 100 rows
   * and orders them by Agent.id — an opaque random string — so a caller
   * treating its result as "all agents" silently gets an arbitrary
   * subset in a meaningless order. Anything populating a picker needs
   * this instead. The page cap stops a broken cursor looping forever. */
  listAllAgents: async (plane?: 'Data' | 'App' | 'Snippet'): Promise<Agent[]> => {
    const out: Agent[] = [];
    let cursor: string | null = null;
    for (let page = 0; page < 50; page++) {
      const qs = new URLSearchParams();
      if (plane) qs.set('plane', plane);
      qs.set('limit', '100');
      if (cursor) qs.set('cursor', cursor);
      const res: Page<Agent> = await reqPaged<Agent>(`/api/agents?${qs.toString()}`);
      out.push(...res.items);
      if (!res.hasMore || !res.nextCursor) break;
      cursor = res.nextCursor;
    }
    return out;
  },
  listPublishedForUser: (invokingUser: string) => req<Agent[]>(`/api/agents/published-for-user?invoking_user=${encodeURIComponent(invokingUser)}`),
  getAgent: (id: string) => req<Agent>(`/api/agents/${id}`),
  createAgent: (body: { name: string; subtype: string; owner?: string; app_type?: string; template_id?: string; template_flow?: any; plane?: 'App' | 'Snippet' }) =>
    req<Agent>('/api/agents', { method: 'POST', body: JSON.stringify(body) }),
  generateAgent: (body: { prompt: string; name: string; subtype?: string; owner?: string; app_type?: string }) =>
    req<Agent>('/api/agents/generate', { method: 'POST', body: JSON.stringify(body) }),
  insertSnippet: (targetAgentId: string, snippetId: string, x: number, y: number, ifMatch?: number) =>
    req<Agent>(`/api/agents/${targetAgentId}/insert-snippet`, { method: 'POST', body: JSON.stringify({ snippet_id: snippetId, x, y }), ...(ifMatch != null ? { headers: { 'If-Match': String(ifMatch) } } : {}) }),
  setAgentMeta: (agentId: string, body: { name?: string; tags?: string[]; description?: string; input_variables?: AgentInputVariable[] }) =>
    req<Agent>(`/api/agents/${agentId}/meta`, { method: 'PATCH', body: JSON.stringify(body) }),
  setAgentFeatures: (agentId: string, features: AgentFeatures) =>
    req<Agent>(`/api/agents/${agentId}/features`, { method: 'PATCH', body: JSON.stringify({ features }) }),
  // Show or hide the UCS check as a step after the Trigger. It does NOT
  // enable or disable entitlement checking — that always runs (see the
  // endpoint's own docstring). The rewiring lives on the server because
  // it is edge surgery on a shared revision.
  setUcsCheck: (agentId: string, enabled: boolean, ifMatch?: number) =>
    req<Agent>(`/api/agents/${agentId}/ucs-check`, {
      method: 'PATCH', body: JSON.stringify({ enabled }),
      ...(ifMatch != null ? { headers: { 'If-Match': String(ifMatch) } } : {}),
    }),
  addNode: (agentId: string, body: { type: string; x: number; y: number; sub?: string; config?: any; parent_id?: string | null }, ifMatch?: number) =>
    req<Agent>(`/api/agents/${agentId}/nodes`, { method: 'POST', body: JSON.stringify(body), ...(ifMatch != null ? { headers: { 'If-Match': String(ifMatch) } } : {}) }),
  moveNode: (agentId: string, nodeId: string, x: number, y: number, ifMatch?: number) =>
    req<Agent>(`/api/agents/${agentId}/nodes/${nodeId}/position`, { method: 'PATCH', body: JSON.stringify({ x, y }), ...(ifMatch != null ? { headers: { 'If-Match': String(ifMatch) } } : {}) }),
  updateNodeConfig: (agentId: string, nodeId: string, config: any, ifMatch?: number) =>
    req<Agent>(`/api/agents/${agentId}/nodes/${nodeId}/config`, { method: 'PATCH', body: JSON.stringify({ config }), ...(ifMatch != null ? { headers: { 'If-Match': String(ifMatch) } } : {}) }),
  deleteNode: (agentId: string, nodeId: string, ifMatch?: number) =>
    req<Agent>(`/api/agents/${agentId}/nodes/${nodeId}`, { method: 'DELETE', ...(ifMatch != null ? { headers: { 'If-Match': String(ifMatch) } } : {}) }),
  addEdge: (agentId: string, source: string, target: string, sourceHandle?: string | null, ifMatch?: number) =>
    req<Agent>(`/api/agents/${agentId}/edges`, { method: 'POST', body: JSON.stringify({ source, target, source_handle: sourceHandle ?? null }), ...(ifMatch != null ? { headers: { 'If-Match': String(ifMatch) } } : {}) }),
  deleteEdge: (agentId: string, edgeId: string, ifMatch?: number) =>
    req<Agent>(`/api/agents/${agentId}/edges/${edgeId}`, { method: 'DELETE', ...(ifMatch != null ? { headers: { 'If-Match': String(ifMatch) } } : {}) }),
  // The publishing identity is sent explicitly. Publish runs a UCS
  // entitlement check for the invoking user as well as the agent's own
  // service principal, and with nothing sent the backend fell back to
  // the placeholder "user:demo" — a principal that can hold no grant,
  // so the user half of every check failed no matter how the catalog
  // was configured.
  // The real principals UCS knows, so the "acting as" chooser can offer
  // identities that can actually hold a grant. It used to offer two
  // hardcoded names that were in no granted group, which made every
  // publish fail the invoking-user half of the entitlement check.
  listUcsPrincipals: () =>
    req<{ principals: { name: string; principal: string; groups: string[]; entitled: boolean }[] }>(
      '/api/integrations/ucs/principals'),
  publishAgent: (agentId: string, invokingUser: string) =>
    req<{ version: string; entitlement_plan: any; audited_as: string }>(
      `/api/agents/${agentId}/publish?invoking_user=${encodeURIComponent(invokingUser)}`,
      { method: 'POST' }),
  runAgent: (agentId: string, input: string, invokingUser: string, inputs?: Record<string, any>, preview?: boolean) =>
    req<{
      run_id?: string; trace_id?: string; execution_mode: string; status: string; output: string | null;
      spans: any[]; fail_class: string | null;
      agent_steps?: { phase: string; output: string }[] | null; agent_strategy?: string | null;
      follow_ups?: string[]; citations?: string[]; structured_output?: Record<string, string> | null;
      hitl_request_id?: string; hitl_rendered_content?: string; hitl_fields?: any[]; hitl_actions?: any[];
      hitl_delivery_status?: Record<string, string>;
    }>(`/api/agents/${agentId}/run`, { method: 'POST', body: JSON.stringify({ input, invoking_user: invokingUser, inputs, preview }) }),
  respondToHitl: (agentId: string, requestId: string, actionId: string, fieldValues: Record<string, string>) =>
    req<{
      run_id?: string; trace_id?: string; status: string; output: string | null; spans: any[]; fail_class: string | null;
      structured_output?: Record<string, string> | null; note?: string | null;
      hitl_request_id?: string; hitl_rendered_content?: string; hitl_fields?: any[]; hitl_actions?: any[];
      hitl_delivery_status?: Record<string, string>;
    }>(`/api/agents/${agentId}/hitl/${requestId}/respond`, {
      method: 'POST', body: JSON.stringify({ action_id: actionId, field_values: fieldValues }),
    }),
  batchRun: (agentId: string, rows: Record<string, string>[], invokingUser: string) =>
    req<BatchRunResult>(`/api/agents/${agentId}/batch-run`, {
      method: 'POST', body: JSON.stringify({ rows, invoking_user: invokingUser }),
    }),
  setMcp: (agentId: string, enabled: boolean) =>
    req<{ enabled: boolean; url: string | null }>(`/api/agents/${agentId}/mcp`, { method: 'PATCH', body: JSON.stringify({ enabled }) }),
  regenerateMcpToken: (agentId: string) =>
    req<{ enabled: boolean; url: string }>(`/api/agents/${agentId}/mcp/regenerate`, { method: 'POST' }),
  exportDsl: (agentId: string) => req<any>(`/api/agents/${agentId}/dsl`),
  importDsl: (dsl: any, asNewAgent: boolean, targetAgentId?: string) =>
    req<Agent>('/api/agents/import-dsl', { method: 'POST', body: JSON.stringify({ dsl, as_new_agent: asNewAgent, target_agent_id: targetAgentId }) }),
  duplicateAgent: (agentId: string) => req<Agent>(`/api/agents/${agentId}/duplicate`, { method: 'POST' }),
  // Draft agents only — the API answers 409 for a staged or published one.
  deleteAgent: (agentId: string) =>
    req<{ deleted: string }>(`/api/agents/${agentId}`, { method: 'DELETE' }),
  setPublishSurface: (agentId: string, surface: 'web_app' | 'api', enabled: boolean) =>
    req<Agent>(`/api/agents/${agentId}/publish-surface`, { method: 'PATCH', body: JSON.stringify({ surface, enabled }) }),
  listAgentVersions: (agentId: string) => req<AgentVersion[]>(`/api/agents/${agentId}/versions`),
  restoreAgentVersion: (agentId: string, versionId: number) =>
    req<Agent>(`/api/agents/${agentId}/versions/${versionId}/restore`, { method: 'POST' }),
  retryUcsSync: (agentId: string) => req<Agent>(`/api/agents/${agentId}/ucs/sync`, { method: 'POST' }),
  // The role catalogue is served rather than restated here: a role
  // model duplicated in two places drifts, and the half that drifts is
  // always the one people read.
  getAgentRoleCatalogue: () => req<AgentRoleDef[]>('/api/agents/roles/catalogue'),
  grantUcsAccess: (agentId: string, body: { group_name: string; group_source?: string; role?: string; tier?: string }) =>
    req<{ status: string; group: string; role: string }>(`/api/agents/${agentId}/ucs/grant`, { method: 'POST', body: JSON.stringify(body) }),
  revokeUcsAccess: (agentId: string, body: { group_name: string; group_source?: string; role?: string; tier?: string }) =>
    req<{ status: string; group: string; role: string }>(`/api/agents/${agentId}/ucs/revoke`, { method: 'POST', body: JSON.stringify(body) }),
  // run_id is the provenance anchor: with it the backend derives the
  // saved input/output/version from the real Run and its Trace and
  // ignores whatever this client claims. Without it the row is
  // recorded as a manual save, which is honest but not an execution
  // record.
  saveResult: (agentId: string, body: { invoking_user: string; run_id?: string | null; input?: string; output?: string | null; structured_output?: Record<string, string> | null }) =>
    req<SavedResult>(`/api/agents/${agentId}/saved-results`, { method: 'POST', body: JSON.stringify(body) }),
  // The owner is required now: listing used to return every user's rows
  // for the agent, and delete accepted any row id, so users of the same
  // agent were not isolated from each other at all.
  listSavedResults: (agentId: string, invokingUser: string) =>
    req<SavedResult[]>(
      `/api/agents/${agentId}/saved-results?invoking_user=${encodeURIComponent(invokingUser)}`),
  deleteSavedResult: (agentId: string, resultId: number, invokingUser: string) =>
    req<{ deleted: boolean }>(
      `/api/agents/${agentId}/saved-results/${resultId}?invoking_user=${encodeURIComponent(invokingUser)}`,
      { method: 'DELETE' }),
  testWebhook: (token: string, payload: Record<string, any>) =>
    req<{ run_id: string; trace_id: string; status: string; output: string | null; spans: any[]; fail_class: string | null }>(
      `/api/webhooks/${token}`, { method: 'POST', body: JSON.stringify(payload) }),
  listEnvVars: (agentId: string) => req<EnvVar[]>(`/api/agents/${agentId}/env`),
  createEnvVar: (agentId: string, body: { key: string; value: string; type: string; description?: string }) =>
    req<EnvVar>(`/api/agents/${agentId}/env`, { method: 'POST', body: JSON.stringify(body) }),
  updateEnvVar: (agentId: string, varId: number, body: { value?: string; description?: string }) =>
    req<EnvVar>(`/api/agents/${agentId}/env/${varId}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteEnvVar: (agentId: string, varId: number) =>
    req<{ deleted: number }>(`/api/agents/${agentId}/env/${varId}`, { method: 'DELETE' }),
  getAgentLogs: (agentId: string) => req<AgentLog[]>(`/api/agents/${agentId}/logs`),
  getAgentRoles: (agentId: string) => req<Role[]>(`/api/agents/${agentId}/roles`),
  getAgentTraces: (agentId: string) => req<Trace[]>(`/api/agents/${agentId}/traces`),

  // templates
  /** Published gallery — safe for New Agent and install/use surfaces. */
  listTemplates: () => req<Template[]>('/api/templates'),
  /** Authoring view — includes drafts and published templates with unpublished edits. */
  listManagedTemplates: () => req<Template[]>('/api/templates/manage'),
  listTemplateVersions: (id: string) => req<TemplateVersion[]>(`/api/templates/${encodeURIComponent(id)}/versions`),
  saveAsTemplate: (body: { agent_id: string; name: string; source: 'platform' | 'private'; desc?: string; category?: string; vertical?: string; tags?: string[] }) =>
    req<SaveTemplateResult>('/api/templates', { method: 'POST', body: JSON.stringify(body) }),
  updateTemplate: (id: string, revision: number, body: { name?: string; desc?: string; category?: string; vertical?: string; tags?: string[]; refresh_from_agent_id?: string }) =>
    req<SaveTemplateResult>(`/api/templates/${encodeURIComponent(id)}`, {
      method: 'PUT', headers: { 'If-Match': String(revision) }, body: JSON.stringify(body),
    }),
  publishTemplate: (id: string, revision: number) =>
    req<PublishTemplateResult>(`/api/templates/${encodeURIComponent(id)}/publish`, {
      method: 'POST', headers: { 'If-Match': String(revision) }, body: JSON.stringify({}),
    }),
  deleteTemplate: (id: string) =>
    req<{ deleted: string; archived?: boolean }>(`/api/templates/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  duplicateTemplate: (id: string, body: { name?: string; state?: 'published' | 'draft' } = {}) =>
    req<Template>(`/api/templates/${encodeURIComponent(id)}/duplicate`, { method: 'POST', body: JSON.stringify(body) }),
  exportTemplate: (id: string, state: 'published' | 'draft' = 'published') =>
    req<any>(`/api/templates/${encodeURIComponent(id)}/export?state=${state}`),
  importTemplate: (pkg: any, name?: string) =>
    req<SaveTemplateResult>('/api/templates/import', { method: 'POST', body: JSON.stringify({ package: pkg, ...(name ? { name } : {}) }) }),
  instantiateTemplate: (templateId: string, body: { name?: string; owner?: string; invoking_user?: string; publish: boolean }) =>
    req<{ agent: Agent; published: boolean; publish_error: string | null }>(
      `/api/templates/${encodeURIComponent(templateId)}/instantiate`, { method: 'POST', body: JSON.stringify(body) }),

  // runs
  listRuns: (status?: string) => req<Run[]>(`/api/runs${status && status !== 'all' ? `?status=${status}` : ''}`),
  /** One keyset page of runs, newest first, with the cursor for the next. */
  listRunsPage: (opts: { status?: string; cursor?: string | null; limit?: number }) => {
    const qs = new URLSearchParams();
    if (opts.status && opts.status !== 'all') qs.set('status', opts.status);
    if (opts.cursor) qs.set('cursor', opts.cursor);
    qs.set('limit', String(opts.limit ?? 25));
    return reqPaged<Run>(`/api/runs?${qs.toString()}`);
  },
  getRun: (id: string) => req<Run>(`/api/runs/${id}`),

  // traces
  // Filters are server-side because this endpoint is paginated: a
  // client-side filter would narrow only the page already fetched and
  // hide every match on later pages.
  listTraces: (agentId?: string, fromDate?: string, toDate?: string) => {
    const qs = new URLSearchParams();
    if (agentId && agentId !== 'all') qs.set('agent_id', agentId);
    if (fromDate) qs.set('from_date', fromDate);
    if (toDate) qs.set('to_date', toDate);
    const q = qs.toString();
    return req<Trace[]>(`/api/traces${q ? `?${q}` : ''}`);
  },
  /** One page of traces, with the cursor needed to fetch the next. */
  listTracesPage: (opts: {
    agentId?: string; fromDate?: string; toDate?: string;
    cursor?: string | null; limit?: number;
  }) => {
    const qs = new URLSearchParams();
    if (opts.agentId && opts.agentId !== 'all') qs.set('agent_id', opts.agentId);
    if (opts.fromDate) qs.set('from_date', opts.fromDate);
    if (opts.toDate) qs.set('to_date', opts.toDate);
    if (opts.cursor) qs.set('cursor', opts.cursor);
    qs.set('limit', String(opts.limit ?? 25));
    return reqPaged<Trace>(`/api/traces?${qs.toString()}`);
  },
  getTrace: (id: string) => req<Trace>(`/api/traces/${id}`),
  getTraceSpans: (id: string) => req<TraceSpansResponse>(`/api/traces/${id}/spans`),

  // alerts
  listAlerts: (state?: string) => req<Alert[]>(`/api/alerts${state ? `?state=${state}` : ''}`),
  // The paged form. `listAlerts` stays for the places that only need a
  // count, and returns the first page like every other bare list call.
  listAlertsPage: (opts: { state?: string; cursor?: string | null; limit?: number;
                           fromDate?: string; toDate?: string }) => {
    const qs = new URLSearchParams();
    if (opts.state) qs.set('state', opts.state);
    if (opts.cursor) qs.set('cursor', opts.cursor);
    if (opts.fromDate) qs.set('from_date', opts.fromDate);
    if (opts.toDate) qs.set('to_date', opts.toDate);
    qs.set('limit', String(opts.limit ?? 25));
    return reqPaged<Alert>(`/api/alerts?${qs.toString()}`);
  },
  getAlert: (id: string) => req<Alert>(`/api/alerts/${id}`),
  acknowledgeAlert: (id: string, by: string) =>
    req<Alert>(`/api/alerts/${id}/acknowledge`, { method: 'POST', body: JSON.stringify({ by }) }),
  resolveAlert: (id: string, by: string) =>
    req<Alert>(`/api/alerts/${id}/resolve`, { method: 'POST', body: JSON.stringify({ by }) }),

  // alert destinations — the Slack/Teams channels alerts are sent to
  // alert rules — what the platform would raise from real run data
  listAlertRules: () => req<AlertRule[]>('/api/alerts/rules'),
  listAlertRulesPage: (opts: { cursor?: string | null; limit?: number;
                               fromDate?: string; toDate?: string }) => {
    const qs = new URLSearchParams();
    if (opts.cursor) qs.set('cursor', opts.cursor);
    if (opts.fromDate) qs.set('from_date', opts.fromDate);
    if (opts.toDate) qs.set('to_date', opts.toDate);
    qs.set('limit', String(opts.limit ?? 25));
    return reqPaged<AlertRule>(`/api/alerts/rules?${qs.toString()}`);
  },
  getAlertRuleKinds: () => req<{ kinds: AlertRuleKind[] }>('/api/alerts/rules/kinds'),
  createAlertRule: (body: AlertRuleIn) =>
    req<AlertRule>('/api/alerts/rules', { method: 'POST', body: JSON.stringify(body) }),
  updateAlertRule: (id: number, body: AlertRuleIn) =>
    req<AlertRule>(`/api/alerts/rules/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteAlertRule: (id: number) =>
    req<{ deleted: number }>(`/api/alerts/rules/${id}`, { method: 'DELETE' }),
  previewAlertRules: () => req<AlertRulePreview>('/api/alerts/rules/preview'),
  sweepAlertRules: () => req<AlertSweepResult>('/api/alerts/rules/sweep', { method: 'POST' }),

  listAlertDestinations: () => req<AlertDestination[]>('/api/alerts/destinations'),
  listAlertDestinationsPage: (opts: { cursor?: string | null; limit?: number }) => {
    const qs = new URLSearchParams();
    if (opts.cursor) qs.set('cursor', opts.cursor);
    qs.set('limit', String(opts.limit ?? 25));
    return reqPaged<AlertDestination>(`/api/alerts/destinations?${qs.toString()}`);
  },
  createAlertDestination: (body: AlertDestinationIn) =>
    req<AlertDestination>('/api/alerts/destinations', { method: 'POST', body: JSON.stringify(body) }),
  updateAlertDestination: (id: number, body: AlertDestinationIn) =>
    req<AlertDestination>(`/api/alerts/destinations/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteAlertDestination: (id: number) =>
    req<{ deleted: number }>(`/api/alerts/destinations/${id}`, { method: 'DELETE' }),
  // A failed test comes back 200 with the reason — the request was
  // handled, the channel is what refused — so callers read `delivered`.
  testAlertDestination: (id: number) =>
    req<AlertDestination & { delivered: boolean; detail: string }>(
      `/api/alerts/destinations/${id}/test`, { method: 'POST' }),

  // monitoring
  getMetrics: (agentId?: string) => req<{ scope: string; agent_name?: string; metrics: Record<string, string | number | null> }>(
    `/api/monitoring/metrics${agentId ? `?agent_id=${agentId}` : ''}`),
  getEvals: (agentName?: string) => req<Eval[]>(`/api/monitoring/evals${agentName ? `?agent_name=${agentName}` : ''}`),
  getEvalHealth: (agentId?: string) => req<EvalHealth>(`/api/monitoring/eval-health${agentId && agentId !== 'all' ? `?agent_id=${agentId}` : ''}`),
  listEvalSets: (agentId?: string) => req<EvalSet[]>(`/api/evals/sets${agentId ? `?agent_id=${agentId}` : ''}`),
  createEvalSet: (name: string, agentId: string) =>
    req<EvalSet>('/api/evals/sets', { method: 'POST', body: JSON.stringify({ name, agent_id: agentId }) }),
  // The stored transcript for one (agent, invoking user). These rows
  // always existed and the model always read them; nothing exposed
  // them to a client, so reopening the app showed an empty chat while
  // the agent still remembered the conversation.
  // Whether THIS browser's requests arrive with a gateway-verified
  // identity. The answer depends on the path a request takes, not on
  // the deployment: nginx strips the five headers, a dev proxy that
  // injects them does not.
  whoami: () => req<{ verified_identity: string | null;
    gateway_trust_enabled: boolean; reason: string | null }>('/api/auth/whoami'),
  /** Who the gateway says the caller is, and the permissions they hold. The
   *  UI shows pages and actions from these; the API enforces them. */
  me: () => req<Me>('/api/me'),
  getChatHistory: (agentId: string, invokingUser: string) =>
    req<ChatHistoryEntry[]>(`/api/agents/${agentId}/chat-history?invoking_user=${encodeURIComponent(invokingUser)}`),
  clearChatHistory: (agentId: string, invokingUser: string) =>
    req<{ cleared: number }>(`/api/agents/${agentId}/chat-history?invoking_user=${encodeURIComponent(invokingUser)}`, { method: 'DELETE' }),
  getEvalRun: (runId: string) =>
    req<{ run: EvalRunRow; results: EvalResultRow[] }>(`/api/evals/runs/${runId}`),
  listEvalCases: (setId: string) => req<EvalCase[]>(`/api/evals/sets/${setId}/cases`),
  addEvalCase: (setId: string, body: { input: string; expected_output: string; criteria?: string }) =>
    req<EvalCase>(`/api/evals/sets/${setId}/cases`, { method: 'POST', body: JSON.stringify(body) }),
  runEvalSet: (setId: string) =>
    req<{ run: EvalRunRow; results: EvalResultRow[]; rate: string }>(`/api/evals/sets/${setId}/run`, { method: 'POST' }),
  listEvalRuns: (setId: string) => req<EvalRunRow[]>(`/api/evals/sets/${setId}/runs`),
  getHomeStats: () => req<HomeStats>('/api/monitoring/stats'),

  // integrations
  listModelProviders: () => req<ModelProvider[]>('/api/integrations/model-providers'),
  // ML Studio is a separate service: the models this person deployed there,
  // read through the /mlstudio-api route (vite.config.ts in dev; the gateway
  // in a deployment). The browser sends no identity — the proxy/gateway adds
  // it, exactly as for /api. Matched on the gateway user name, which both apps share.
  listMlStudioDeployments: async (userName: string): Promise<MlStudioDeployment[]> => {
    let res: Response;
    try {
      res = await fetch(`${BASE}/mlstudio-api/deployments?deployedBy=${encodeURIComponent(userName)}`);
    } catch {
      throw new ApiError("Couldn't reach ML Studio", 0);
    }
    if (!res.ok) throw new ApiError(`ML Studio answered ${res.status}`, res.status);
    return res.json();
  },
  // §0.115, GAP_REPORT — the real, machine-readable "runtime schema"
  // layer (see node_wiring.py's own module docstring) the Builder's
  // own config panels now query directly, rather than each one hand-
  // maintaining its own, separately-drifting warning text.
  getUnwiredNodeControls: () => req<Record<string, string[]>>('/api/agents/node-schema/unwired-controls'),
  getRuntimeConfig: () => req<RuntimeConfig>('/api/settings/runtime-config'),
  getGuardrailsOverview: () => req<GuardrailsOverview>('/api/guardrails/overview'),
  // Every embedding model selectable for a RAG pipeline, across all
  // configured providers — populates the Settings tab dropdown. A model
  // qualifies by declaring the "Text Embedding" type OR by having an id
  // from a known embedding family, in which case `inferred` is set; see
  // the endpoint's own docstring for why both routes exist.
  listEmbeddingModels: () =>
    req<{ models: { name: string; provider: string | null; api_model_id: string | null;
                    embedding_dimension: number | null; builtin: boolean;
                    available: boolean; inferred?: boolean; types?: string[];
                    note?: string | null }[] }>(
      '/api/integrations/embedding-models'),
  // Whether switching Rerank Model on will genuinely rerank, and why
  // not when it won't. The RAG Settings card used to hardcode this
  // claim and showed a warning even with a working key configured.
  getRerankAvailability: () =>
    req<{ available: boolean; source: string | null; kind: 'llm' | null; reason: string | null;
          catalogued_rerank_models: { name: string; provider: string; api_model_id: string }[];
          models: { name: string; provider: string | null; builtin: boolean;
                    available: boolean; note: string | null }[] }>(
      '/api/integrations/rerank-availability'),
  // Whether an audio file can genuinely be ingested: audio has no text
  // layer, so it has to be transcribed, which needs a speech provider
  // key. Asked before any picker offers a .wav, so the UI never lists
  // a file the upload would then refuse at the very end.
  getTranscriptionAvailability: () =>
    req<{ available: boolean; providers: string[]; extensions: string[]; reason: string | null }>(
      '/api/integrations/transcription-availability'),
  // Where an uploaded file itself would be kept. The Documents tab's
  // "keep the original" checkbox names the destination, and only the
  // server knows whether that is a real S3 bucket or this machine's
  // disk — claiming a bucket that isn't configured would be a label
  // asserting something nobody checked.
  getStorageTarget: () => req<StorageTarget>('/api/integrations/storage-target'),
  // Git integrations (Settings). Tokens are checked against the provider
  // before they are saved and never come back from the server.
  listGitConnections: () =>
    req<{ connections: GitConnection[]; can_store_credentials: boolean }>('/api/git-connections'),
  testGitCredentials: (body: { provider: GitProvider; token: string; base_url?: string; username?: string }) =>
    req<{ ok: boolean; message: string; account?: string; repo_count?: number }>(
      '/api/git-connections/test', { method: 'POST', body: JSON.stringify(body) }),
  createGitConnection: (body: {
    provider: GitProvider; token: string; name?: string; base_url?: string; username?: string;
    default_repo?: string; default_branch?: string; default_folder?: string; commit_on_download?: boolean;
  }) => req<GitConnection>('/api/git-connections', { method: 'POST', body: JSON.stringify(body) }),
  updateGitConnection: (id: string, body: Partial<{
    name: string; token: string; base_url: string; username: string;
    default_repo: string; default_branch: string; default_folder: string; commit_on_download: boolean;
  }>) => req<GitConnection>(`/api/git-connections/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  retestGitConnection: (id: string) =>
    req<GitConnection & { ok: boolean; message: string }>(`/api/git-connections/${id}/test`, { method: 'POST' }),
  deleteGitConnection: (id: string) =>
    req<{ deleted: string }>(`/api/git-connections/${id}`, { method: 'DELETE' }),
  listGitRepos: (id: string) => req<GitRepo[]>(`/api/git-connections/${id}/repos`),
  listGitBranches: (id: string, repo: string) =>
    req<string[]>(`/api/git-connections/${id}/branches?repo=${encodeURIComponent(repo)}`),
  /** The agent's DSL as YAML text, plus the filename the server chose. */
  exportAgentYaml: async (agentId: string): Promise<{ text: string; filename: string }> => {
    const res = await fetch(`${BASE}/api/agents/${encodeURIComponent(agentId)}/export.yaml`).catch((e) => {
      throw new Error(`Couldn't reach the backend at ${BASE} — is it running? (${e instanceof Error ? e.message : 'network error'})`);
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new ApiError(errorMessageFor(body, res.status), res.status, body.detail);
    }
    const cd = res.headers.get('Content-Disposition') || '';
    const filename = /filename="([^"]+)"/.exec(cd)?.[1] || `${agentId}.yaml`;
    return { text: await res.text(), filename };
  },
  commitAgentToGit: (agentId: string, body: { connection_id: string; repo?: string; branch?: string; path?: string; message?: string }) =>
    req<GitCommitResult>(`/api/agents/${encodeURIComponent(agentId)}/git-commit`, { method: 'POST', body: JSON.stringify(body) }),
  // Really writes, reads back and deletes one small object with these
  // credentials. Nothing weaker is worth showing: head_bucket passes
  // for a bucket the key cannot write to.
  testStorageDestination: (body: {
    bucket: string; region?: string; endpoint_url?: string;
    access_key_id?: string; secret_access_key?: string;
  }) => req<{ ok: boolean; message: string }>(
    '/api/integrations/storage-destinations/test', { method: 'POST', body: JSON.stringify(body) }),
  // Tested server-side before it is stored, so a destination that
  // cannot be written to never reaches the list.
  createStorageDestination: (body: {
    name?: string; bucket: string; region?: string; endpoint_url?: string;
    access_key_id?: string; secret_access_key?: string; make_default?: boolean;
  }) => req<StorageDestination & { verified: string }>(
    '/api/integrations/storage-destinations', { method: 'POST', body: JSON.stringify(body) }),
  setDefaultStorageDestination: (id: string) =>
    req<StorageTarget>(`/api/integrations/storage-destinations/${id}/default`, { method: 'POST' }),
  testStoredStorageDestination: (id: string) =>
    req<{ ok: boolean; message: string }>(
      `/api/integrations/storage-destinations/${id}/test`, { method: 'POST' }),
  deleteStorageDestination: (id: string) =>
    req<{ deleted: string }>(`/api/integrations/storage-destinations/${id}`, { method: 'DELETE' }),
  // Where a browser-picked folder actually lives, resolved by the API
  // looking on its OWN filesystem. Only meaningful when the API runs on
  // the same machine as the browser (local dev); `found: false`
  // otherwise, and the caller must keep saying the root is assumed.
  // `size` is what makes a BARE filename resolvable: the single-file
  // picker exposes only the name, which can match many files, but name
  // plus exact byte size almost never does. `ambiguous` means several
  // real files matched and the caller must choose rather than have one
  // guessed for them.
  resolveLocalPath: (relativePath: string, size?: number) =>
    req<{
      found: boolean; root?: string; absolute_path?: string; reason?: string; scanned?: number;
      ambiguous?: boolean; matches?: { root: string; absolute_path: string; size: number }[];
      // true when the API itself runs in a container: no local path can
      // EVER resolve through it, which is a different situation from a
      // file simply not being found.
      containerised?: boolean;
    }>('/api/integrations/resolve-local-path',
      { method: 'POST', body: JSON.stringify({ relative_path: relativePath, size }) }),
  // Every chat model selectable in the Builder: the built-in catalog
  // plus everything registered on the Integrations page, each flagged
  // with whether it can actually run. Mirrors listEmbeddingModels,
  // which is what the RAG page's own model dropdowns read.
  listLlmModels: () =>
    req<{ models: LlmModelOption[] }>('/api/integrations/llm-models'),
  // Client secrets of ML Studio endpoints, so their LLMs can run in nodes.
  // Write-only: the list says which models have one, never the value.
  listMlStudioCredentials: () => req<{ models: string[] }>('/api/integrations/mlstudio-credentials'),
  setMlStudioCredential: (modelId: string, clientSecret: string) =>
    req<{ model_id: string; configured: boolean }>(`/api/integrations/mlstudio-credentials/${encodeURIComponent(modelId)}`, {
      method: 'PUT', body: JSON.stringify({ client_secret: clientSecret }),
    }),
  deleteMlStudioCredential: (modelId: string) =>
    req<{ model_id: string; configured: boolean }>(`/api/integrations/mlstudio-credentials/${encodeURIComponent(modelId)}`, { method: 'DELETE' }),

  listProviderModels: (provider: string) =>
    req<{ provider: string; runtime_supported: boolean;
          builtin: { name: string; api_model_id: string; builtin: boolean }[];
          custom: { name: string; api_model_id: string; types?: string[];
                    input_price_per_mtok?: number | null;
                    output_price_per_mtok?: number | null; builtin: boolean }[] }>(
      `/api/integrations/model-providers/${encodeURIComponent(provider)}/models`),
  addProviderModel: (provider: string, body: { name: string; api_model_id: string;
      types?: string[]; input_price_per_mtok?: number; output_price_per_mtok?: number }) =>
    req<any>(`/api/integrations/model-providers/${encodeURIComponent(provider)}/models`,
      { method: 'POST', body: JSON.stringify(body) }),
  deleteProviderModel: (provider: string, modelName: string) =>
    req<{ deleted: string }>(
      `/api/integrations/model-providers/${encodeURIComponent(provider)}/models/${encodeURIComponent(modelName)}`,
      { method: 'DELETE' }),
  configureModelProvider: (name: string, body: any) =>
    req<ModelProvider>(`/api/integrations/model-providers/${encodeURIComponent(name)}/api-key`, {
      method: 'POST', body: JSON.stringify(body),
    }),
  listTools: () => req<Tool[]>('/api/integrations/tools'),
  getToolActions: (name: string) => req<ToolAction[]>(`/api/integrations/tools/${encodeURIComponent(name)}/actions`),
  listMcpServers: () => req<MCPServer[]>('/api/integrations/mcp-servers'),
  addMcpServer: (body: any) => req<MCPServer>('/api/integrations/mcp-servers', { method: 'POST', body: JSON.stringify(body) }),
  listDataSources: () => req<DataSource[]>('/api/integrations/data-sources'),
  listKnowledgeBases: () => req<KnowledgeBase[]>('/api/integrations/knowledge-bases'),
  createKnowledgeBase: (body: { name: string; provider: string; config: Record<string, any>;
      // Reuse another KB's stored connection secrets — reads mask them,
      // so the server copies what only it can decrypt.
      copy_credentials_from?: string }) =>
    req<KnowledgeBase>('/api/integrations/knowledge-bases', { method: 'POST', body: JSON.stringify(body) }),
  updateKbSettings: (kbId: string, settings: KBSettings) =>
    req<KnowledgeBase>(`/api/integrations/knowledge-bases/${kbId}/settings`, { method: 'PATCH', body: JSON.stringify(settings) }),
  regenerateKbApiKey: (kbId: string) =>
    req<KnowledgeBase>(`/api/integrations/knowledge-bases/${kbId}/api/regenerate-key`, { method: 'POST' }),
  // Separate key from the retrieval one on purpose — this surface writes.
  regenerateKbUploadApiKey: (kbId: string) =>
    req<KnowledgeBase>(`/api/integrations/knowledge-bases/${kbId}/api/upload-key/regenerate`, { method: 'POST' }),
  callKbUploadApi: async (kbId: string, apiKey: string, file: File, opts: { maxWords?: number; overlapWords?: number } = {}) => {
    await checkUploadSize(file);
    const form = new FormData();
    form.append('file', file);
    if (opts.maxWords != null) form.append('max_words', String(opts.maxWords));
    if (opts.overlapWords != null) form.append('overlap_words', String(opts.overlapWords));
    // No Content-Type header: the browser must set its own multipart
    // boundary, and naming it here would produce a body the server
    // cannot parse.
    return req<{ document_id: string; chunks_inserted: number; table: string }>(
      `/api/integrations/knowledge-bases/${kbId}/api/upload`,
      { method: 'POST', headers: { Authorization: `Bearer ${apiKey}` }, body: form },
    );
  },
  // The Upload API's data-source counterpart: names files that already
  // live in a connected Data Source instead of POSTing bytes, so the
  // command can carry the file's whole path rather than a bare name.
  callKbUploadFromSourceApi: (kbId: string, apiKey: string, body: {
    source: string; file_ids: string[]; folder_paths?: string[];
    max_words?: number; overlap_words?: number; metadata?: string;
  }) => req<SourceIngestResult>(`/api/integrations/knowledge-bases/${kbId}/api/upload-from-source`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify(body),
  }),
  // Puts a file into the platform's own upload store so a command can
  // name it by id instead of by a local path the browser won't reveal.
  stageUpload: async (file: File, destinationId?: string) => {
    await checkUploadSize(file);
    const form = new FormData();
    form.append('file', file);
    const query = destinationId ? `?destination_id=${encodeURIComponent(destinationId)}` : '';
    return req<{ id: string; filename: string; size: number; url: string; expires_at: string | null }>(
      `/api/uploads${query}`, { method: 'POST', body: form });
  },
  callKbUploadFromStoreApi: (kbId: string, apiKey: string, body: {
    upload_id: string; max_words?: number; overlap_words?: number; metadata?: string;
  }) => req<{ document_id: string; chunks_inserted: number; table: string; upload_id: string }>(
    `/api/integrations/knowledge-bases/${kbId}/api/upload-from-store`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify(body),
    }),
  callKbRetrievalApi: (kbId: string, apiKey: string, query: string, topK = 5) =>
    req<{ results: KBRetrieveResult[]; rerank_note?: string | null }>(`/api/integrations/knowledge-bases/${kbId}/api/retrieve`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` }, body: JSON.stringify({ query, top_k: topK }),
    }),
  testKnowledgeBase: (id: string) =>
    req<KnowledgeBase & { test_result: { ok: boolean; detail?: string; error?: string } }>(
      `/api/integrations/knowledge-bases/${id}/test`, { method: 'POST' }),
  // Edit a knowledge base's connection in place. A secret left blank or
  // sent as the mask keeps whatever is stored — reads never expose it.
  // confirm_relocate is required to repoint a KB that already lists
  // documents at different storage.
  updateKbConnection: (id: string, config: Record<string, any>, confirmRelocate = false) =>
    req<KnowledgeBase & { relocated?: string[]; documents_affected?: number }>(
      `/api/integrations/knowledge-bases/${id}/connection`,
      { method: 'PATCH', body: JSON.stringify({ config, confirm_relocate: confirmRelocate }) }),
  deleteKnowledgeBase: (id: string) =>
    req<{ deleted: string }>(`/api/integrations/knowledge-bases/${id}`, { method: 'DELETE' }),
  ingestDocument: async (kbId: string, file: File, opts: { table?: string; maxWords?: number; overlapWords?: number; metadata?: Record<string, any>; keepOriginal?: boolean; storageDestinationId?: string }) => {
    await checkUploadSize(file);
    const form = new FormData();
    form.append('file', file);
    if (opts.table) form.append('table', opts.table);
    if (opts.maxWords != null) form.append('max_words', String(opts.maxWords));
    if (opts.overlapWords != null) form.append('overlap_words', String(opts.overlapWords));
    if (opts.metadata) form.append('metadata', JSON.stringify(opts.metadata));
    // Keep the uploaded file itself, not only the text extracted from
    // it — in the same object storage the Upload API tab stages to.
    if (opts.keepOriginal) form.append('keep_original', 'true');
    if (opts.storageDestinationId) form.append('storage_destination_id', opts.storageDestinationId);
    return req<KBIngestResult>(`/api/integrations/knowledge-bases/${kbId}/ingest`, { method: 'POST', body: form });
  },
  listKbDocuments: (kbId: string) => req<KnowledgeDocument[]>(`/api/integrations/knowledge-bases/${kbId}/documents`),
  setKbDocumentStatus: (kbId: string, docId: string, status: 'enabled' | 'disabled') =>
    req<KnowledgeDocument>(`/api/integrations/knowledge-bases/${kbId}/documents/${docId}`, { method: 'PATCH', body: JSON.stringify({ status }) }),
  // Writes metadata onto every chunk of an already-ingested document —
  // previously only possible at upload time.
  setKbDocumentMetadata: (kbId: string, docId: string, metadata: Record<string, any>) =>
    req<KnowledgeDocument>(`/api/integrations/knowledge-bases/${kbId}/documents/${docId}`, { method: 'PATCH', body: JSON.stringify({ metadata }) }),
  deleteKbDocument: (kbId: string, docId: string) =>
    req<{ deleted: string }>(`/api/integrations/knowledge-bases/${kbId}/documents/${docId}`, { method: 'DELETE' }),
  listKbChunks: (kbId: string, docId: string) => req<KnowledgeChunk[]>(`/api/integrations/knowledge-bases/${kbId}/documents/${docId}/chunks`),
  updateKbChunk: (kbId: string, chunkId: number, body: { content?: string; enabled?: boolean }) =>
    req<KnowledgeChunk>(`/api/integrations/knowledge-bases/${kbId}/chunks/${chunkId}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteKbChunk: (kbId: string, chunkId: number) =>
    req<{ deleted: number }>(`/api/integrations/knowledge-bases/${kbId}/chunks/${chunkId}`, { method: 'DELETE' }),
  listKbMetadataFields: (kbId: string) => req<KnowledgeMetadataField[]>(`/api/integrations/knowledge-bases/${kbId}/metadata-fields`),
  listKbMetadataKeys: (kbId: string) => req<{ keys: string[] }>(`/api/integrations/knowledge-bases/${kbId}/metadata-keys`),
  addKbMetadataField: (kbId: string, name: string, type: string) =>
    req<KnowledgeMetadataField>(`/api/integrations/knowledge-bases/${kbId}/metadata-fields`, { method: 'POST', body: JSON.stringify({ name, type }) }),
  renameKbMetadataField: (kbId: string, fieldId: string, name: string, type: string) =>
    req<KnowledgeMetadataField>(`/api/integrations/knowledge-bases/${kbId}/metadata-fields/${fieldId}`, { method: 'PATCH', body: JSON.stringify({ name, type }) }),
  deleteKbMetadataField: (kbId: string, fieldId: string) =>
    req<{ deleted: string }>(`/api/integrations/knowledge-bases/${kbId}/metadata-fields/${fieldId}`, { method: 'DELETE' }),
  runKbRetrievalTest: (kbId: string, query: string, topK = 5) =>
    req<{ results: KnowledgeRetrievalTestResult[]; rerank_note?: string | null }>(`/api/integrations/knowledge-bases/${kbId}/retrieval-test`, {
      method: 'POST', body: JSON.stringify({ query, top_k: topK }),
    }),
  getKbRetrievalTestHistory: (kbId: string) => req<KnowledgeRetrievalTestHistoryEntry[]>(`/api/integrations/knowledge-bases/${kbId}/retrieval-test-history`),
  configureDataSourceOAuth: (name: string, body: any) =>
    req<DataSource>(`/api/integrations/data-sources/${encodeURIComponent(name)}/oauth`, { method: 'POST', body: JSON.stringify(body) }),
  configureDataSourceApiKey: (name: string, body: any) =>
    req<DataSource>(`/api/integrations/data-sources/${encodeURIComponent(name)}/api-key`, { method: 'POST', body: JSON.stringify(body) }),
  disconnectDataSource: (name: string) =>
    req<DataSource>(`/api/integrations/data-sources/${encodeURIComponent(name)}/credentials`, { method: 'DELETE' }),
  // `path` is whatever `path` an earlier entry carried, so nesting
  // works to any depth without the UI knowing a source's addressing.
  browseDataSource: (name: string, path = '') =>
    req<DataSourceBrowseResult>(
      `/api/integrations/data-sources/${encodeURIComponent(name)}/browse?path=${encodeURIComponent(path)}`),
  ingestFromDataSource: (kbId: string, body: {
    source: string; file_ids: string[]; folder_paths?: string[]; table?: string;
    max_words?: number; overlap_words?: number; metadata?: string; force_reindex?: boolean;
    keep_original?: boolean; storage_destination_id?: string;
  }) => req<SourceIngestResult>(`/api/integrations/knowledge-bases/${kbId}/ingest-from-source`,
    { method: 'POST', body: JSON.stringify(body) }),
};
