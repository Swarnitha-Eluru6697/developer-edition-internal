import { ucsName, ucsConfig, buildAdminHeaders, buildIdentityHeaders, ucsServiceIdentity, UCS_SERVICE_PERMISSIONS, UcsIdentity } from "./config";

// Matches the real, verified app/routers/admin.py + app/schemas/models.py
// from the uploaded UCS codebase — request/response shapes confirmed by
// reading the actual Pydantic schemas, not assumed. Endpoints requiring
// an admin permission are marked ADMIN below; those use buildAdminHeaders()
// (ML Studio's service name + ucs:catalogs:manage,ucs:rbac:manage). Everything else uses buildIdentityHeaders() (the
// X-Tenant-Id/X-User-Name/X-User-Groups header scheme identity.py's
// principal_from_headers reads) — live-verified against a real running
// instance of the uploaded code, including the exact allow/deny semantics.

export class UcsError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.status = status;
  }
}

async function ucsRequest<T>(method: string, path: string, headers: Record<string, string>, body?: unknown): Promise<T> {
  if (!ucsConfig.enabled) throw new UcsError("UCS is disabled (UCS_ENABLED=false)", 503);
  const res = await fetch(`${ucsConfig.baseUrl}${path}`, {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const json = text ? JSON.parse(text) : {};
  if (!res.ok) {
    // admin.py's _gravitino_http_error unwraps Gravitino's own "message"
    // field into the response body on failure — surface it as-is.
    throw new UcsError(json.detail || json.message || text || `UCS ${method} ${path} -> ${res.status}`, res.status);
  }
  return json as T;
}

// ---- Health — GET /health (real endpoint; the old client's /api/version
// guess doesn't exist in the real codebase) ----
export async function ucsHealthy(): Promise<boolean> {
  if (!ucsConfig.enabled) return false;
  try {
    const res = await fetch(`${ucsConfig.baseUrl}/health`);
    return res.ok;
  } catch {
    return false;
  }
}

// ---- Catalogs — ADMIN. Matches CatalogCreate exactly. ----
export interface UcsCatalogCreate {
  name: string;
  engine: "iceberg" | "jdbc-postgresql" | "kafka" | "model" | "fileset";
  comment?: string;
  properties?: Record<string, unknown>;
  enable_credential_vending?: boolean;
}
export async function registerCatalog(req: UcsCatalogCreate): Promise<unknown> {
  return ucsRequest("POST", "/api/v1/admin/catalogs", buildAdminHeaders(), req);
}

// ---- Models — ADMIN. Matches ModelCreate / ModelVersionCreate exactly —
// catalog/schema_name/name is Gravitino's real 3-level namespace, not a
// flattened name. ----
export interface RegisterModelResult { status: string; model: string; raw: unknown }
/** UCS (Gravitino) model and version properties are a string -> string map: nested
 *  objects and nulls made a version registration fail with "Malformed json request". */
function ucsProperties(props: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined) continue;
    out[k] = typeof v === "string" ? v : JSON.stringify(v);
  }
  return out;
}

export async function registerModel(name: string, comment: string, properties: Record<string, unknown> = {},
                                    schema: string = ucsConfig.modelSchema): Promise<RegisterModelResult> {
  return ucsRequest("POST", "/api/v1/admin/models", buildAdminHeaders(), {
    catalog: ucsConfig.modelCatalog,
    schema_name: schema,
    name: ucsName(name),
    comment,
    // The ML Studio name, kept so a renamed-for-UCS model (dots -> underscores) maps back.
    properties: ucsProperties({ mlStudioName: name, ...properties }),
  });
}

export interface RegisterModelVersionResult { status: string; model: string; uri: string; raw: unknown }
export async function registerModelVersion(
  model: string,
  uri: string,
  aliases: string[],
  comment: string,
  properties: Record<string, unknown> = {},
  schema: string = ucsConfig.modelSchema
): Promise<RegisterModelVersionResult> {
  return ucsRequest("POST", "/api/v1/admin/models/versions", buildAdminHeaders(), {
    catalog: ucsConfig.modelCatalog,
    schema_name: schema,
    model: ucsName(model),
    uri,
    aliases,
    comment,

    properties: ucsProperties(properties),
  });
}

// ---- RBAC — ADMIN. Matches RoleCreate / GroupBind exactly. ----
export interface UcsRoleCreate {
  role_name: string;
  catalog: string;
  schema_name?: string;
  object_name?: string;
  object_type?: "catalog" | "schema" | "table" | "topic" | "model" | "fileset";
  access_level?: "select" | "writer" | "owner";
}
export async function createRole(req: UcsRoleCreate): Promise<unknown> {
  return ucsRequest("POST", "/api/v1/admin/rbac/roles", buildAdminHeaders(), req);
}
/** Create a schema in a catalog (UCS proxies to Gravitino, which only UCS can reach).
 *  409 = it already exists. */
export async function createSchema(catalog: string, name: string, comment = ""): Promise<unknown> {
  return ucsRequest("POST", "/api/v1/admin/schemas", buildAdminHeaders(), { catalog, name, comment });
}
export async function bindGroupToRole(groupName: string, roleName: string, groupSource: "oidc" | "custom" = "custom"): Promise<unknown> {
  return ucsRequest("POST", "/api/v1/admin/rbac/groups/bind", buildAdminHeaders(), {
    group_name: groupName, role_name: roleName, group_source: groupSource,
  });
}
export async function revokeGroupFromRole(groupName: string, roleName: string): Promise<unknown> {
  return ucsRequest("POST", "/api/v1/admin/rbac/groups/revoke", buildAdminHeaders(), { group_name: groupName, role_name: roleName });
}

// ---- Authz check — header-auth (NOT admin). Real per-decision gateway,
// matches AuthzRequest/AuthzDecision + core/rbac_cache.py's decide()
// exactly: `object` is a dotted catalog.schema.object_name FQN, `action`
// is a semantic string (read/write/create/update/...), NOT a raw
// Gravitino privilege name. Live-verified: allow/deny/401-with-no-headers
// all confirmed against a real running instance. ----
export interface AuthzDecision { decision: "allow" | "deny"; reason: string }
export async function checkAuthz(identity: UcsIdentity, action: string, object: string): Promise<AuthzDecision> {
  return ucsRequest("POST", "/api/v1/authz/check", buildIdentityHeaders(identity), { action, object });
}

// ---- Lineage — header-auth. /lineage/events takes a real OpenLineage
// RunEvent (untyped dict on the real side — no schema to match beyond the
// OpenLineage spec itself). Live-verified: ingest -> graph/runs/impact
// round-tripped correctly against a real running instance. ----
export interface OpenLineageDataset { namespace: string; name: string; facets?: Record<string, unknown> }
export interface OpenLineageEvent {
  eventType: "START" | "RUNNING" | "COMPLETE" | "FAIL" | "ABORT";
  run: { runId: string };
  job: { namespace: string; name: string };
  inputs?: OpenLineageDataset[];
  outputs?: OpenLineageDataset[];
}
/** Lineage is written by ML Studio as the producer: its service name and
 *  ucs:lineage-events:create. Who the run was for is in the event itself. */
export async function ingestLineageEvent(event: OpenLineageEvent): Promise<unknown> {
  return ucsRequest("POST", "/api/v1/lineage/events",
                    buildIdentityHeaders(ucsServiceIdentity(UCS_SERVICE_PERMISSIONS.lineageProducer)),
                    event);
}

export interface LineageGraph {
  object: string;
  status: "available" | "not_found";
  upstream: { fqn: string; job: string }[];
  downstream: { fqn: string; job: string }[];
}
export async function getLineageGraph(identity: UcsIdentity, fqn: string, depth = 2): Promise<LineageGraph> {
  return ucsRequest("GET", `/api/v1/lineage/${encodeURIComponent(fqn)}/graph?depth=${depth}`, buildIdentityHeaders(identity));
}

export interface LineageImpact {
  object: string;
  financial_flagged: boolean;
  downstream_count: number;
  financial_downstream: number;
  downstream: { fqn: string; job: string; financial_flagged: boolean }[];
}
export async function getLineageImpact(identity: UcsIdentity, fqn: string): Promise<LineageImpact> {
  return ucsRequest("GET", `/api/v1/lineage/${encodeURIComponent(fqn)}/impact`, buildIdentityHeaders(identity));
}

export interface LineageRun { run: string; job: string; state: string; ts: string | null }
export async function getLineageRuns(identity: UcsIdentity, fqn: string): Promise<{ runs: LineageRun[] }> {
  return ucsRequest("GET", `/api/v1/lineage/${encodeURIComponent(fqn)}/runs`, buildIdentityHeaders(identity));
}

// ---- User-scoped catalog reads — header-auth (NOT admin). Real profile
// (asset_profile_service.build, full=false) and real catalog browse tree. ----
export async function getObjectProfile(identity: UcsIdentity, fqn: string): Promise<Record<string, unknown>> {
  return ucsRequest("GET", `/api/v1/user/objects/${encodeURIComponent(fqn)}/profile`, buildIdentityHeaders(identity));
}
export async function getExplorerTree(identity: UcsIdentity): Promise<{ tree: Record<string, unknown>; as: string }> {
  return ucsRequest("GET", "/api/v1/user/explorer-tree", buildIdentityHeaders(identity));
}
