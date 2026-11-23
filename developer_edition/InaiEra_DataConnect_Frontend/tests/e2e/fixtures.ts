/*
 * Fake DataConnect backend for the browser-driven specs — intercepts every
 * call the app makes (see src/api.ts's request()) via page.route() and
 * serves it from an in-memory store shaped like the real FastAPI responses,
 * instead of requiring a live Postgres/Kafka Connect stack in CI.
 *
 * The backend's per-route permission checks aren't re-implemented here —
 * that's the backend's job and it's covered by its own suite (tests/test_rbac.py,
 * tests/test_gateway_contract.py). This fixture answers GET /me with the
 * permissions of a test persona (below) so the UI can be driven as each one.
 */
import { expect, type Locator, type Page, type Route } from "@playwright/test";
import { PERMISSION_IDS } from "../../src/types/permissions.generated";

// ── Test personas: the Admin Portal's SEEDED DataConnect roles, as the permission lists GET /me returns ──
// Role names exist only here, in test data; the app sees nothing but permissions.
export type RoleName = "Admin" | "Integrator" | "Viewer";
const ALL = [...PERMISSION_IDS] as string[];
export const PERSONAS: Record<RoleName, string[]> = {
  Admin: ALL,
  // Alert rules and channels (webhook URLs are credentials) stay Admin-only; Integrators may acknowledge / resolve (backend docs/auth/PERMISSIONS.md).
  Integrator: ALL.filter((p) => p !== "dataconnect:templates:manage" && p !== "dataconnect:settings:edit" && p !== "dataconnect:alerts:manage"),
  Viewer: ALL.filter((p) => p.endsWith(":view")),
};

export interface MeBody { user: string; userId: string; tenant: string; permissions: string[] }
export const DEFAULT_TENANT = "org_1001";
export function meFor(role: RoleName, user = "Lithish"): MeBody {
  return { user, userId: `u-${user.toLowerCase()}`, tenant: DEFAULT_TENANT, permissions: [...PERSONAS[role]] };
}

export interface BackendState {
  connections: Record<string, unknown>[];
  pipelines: Record<string, unknown>[];
  transforms: Record<string, unknown>[];
  qualityPolicies: Record<string, unknown>[];
  qualityResults: Record<string, unknown>[];
  schemas: Record<string, unknown>[];
  driftApprovals: Record<string, unknown>[];
  templates: Record<string, unknown>[];
  audit: Record<string, unknown>[];
  operations: Record<string, unknown>[];
  discoveryRuns: Record<string, unknown>[];
  discoveryObjects: Record<string, unknown>[];
  /** Mark the next discovery run as truncated (the source had more objects than the backend keeps). */
  discoveryTruncated?: boolean;
  lineageAssets: Record<string, unknown>[];
  lineageEdges: Record<string, unknown>[];
  /** What GET /quality-policies/capabilities answers (NEW-09). Default: what the real backend answers today. "fail" = the endpoint errors. */
  qualityCapabilities?: Record<string, unknown> | "fail";
  /** What GET /me answers (the signed-in user, tenant and permissions). Default: the Admin persona. A number = that HTTP error. */
  me?: MeBody | number;
}

const SUPPORTED = { state: "supported", code: null, message: null, remediation: null, verified: true };
const NOT_IMPLEMENTED = (message: string, remediation: string) => ({ state: "unavailable", code: "NOT_IMPLEMENTED", message, remediation, verified: true });

/** The real backend's answer today: Kafka works; object storage, "drop" and "block" are not implemented. */
export const QUALITY_CAPABILITIES = {
  quarantine_sinks: [
    { id: "kafka", label: "Kafka topic", description: "Failing records are written to the quarantine topic.", availability: SUPPORTED },
    { id: "object_storage", label: "Object storage", description: "Failing records are written to a bucket.",
      availability: NOT_IMPLEMENTED("Quarantining to object storage is reserved for a future backend and is not implemented yet.", "Use the Kafka quarantine topic.") },
  ],
  failure_actions: [
    { id: "route_to_quarantine", label: "Route to quarantine", description: "Failing records are copied to quarantine.", availability: SUPPORTED },
    { id: "log", label: "Log and count only", description: "Failing records are counted and logged.", availability: SUPPORTED },
    { id: "drop", label: "Drop", description: "Discard failing records.",
      availability: NOT_IMPLEMENTED("Dropping failing records is not implemented.", "Use route_to_quarantine or log.") },
    { id: "block", label: "Block the stream", description: "Stop when a record fails.",
      availability: NOT_IMPLEMENTED("Stopping a stream when a record fails is not implemented.", "Use route_to_quarantine with ENFORCE.") },
  ],
};

let seq = 0;
export function nextId(prefix: string): string {
  seq += 1;
  return `${prefix}-${String(seq).padStart(4, "0")}`;
}

/*
 * FE-BUG-10: a faithful mirror of the real backend's connection field
 * schemas (app/services/connection_config_schema.py) — the app now fetches
 * this instead of hardcoding it, so the mock has to actually serve it (this
 * is what drives the "New <type> connection" form's fields and the source-
 * type tile grid; a page.goto("/") -> Connections with none of this would
 * render an empty grid, not a stand-in). Kept faithful to the real field
 * list/required flags rather than a convenient shortcut — postgres
 * genuinely requires username+password server-side (verified against the
 * live backend), and a mock that pretended otherwise would let this suite
 * pass against a form flow the real backend actually rejects.
 */
function databaseFields(defaultPort: number) {
  return [
    { key: "hostname", label: "Host", type: "string", required: true, placeholder: "db.internal" },
    { key: "port", label: "Port", type: "number", default: defaultPort },
    { key: "database", label: "Database", type: "string", required: true },
    { key: "username", label: "Username", type: "string", required: true },
    { key: "password", label: "Password", type: "secret", required: true, persist: false },
    { key: "tables", label: "Tables", type: "list", description: "Table names, or '*' for all" },
  ];
}
export const CONNECTION_CONFIG_SCHEMAS: Record<string, unknown>[] = [
  ...[
    ["postgres", 5432], ["postgresql", 5432], ["mysql", 3306], ["sqlserver", 1433], ["oracle", 1521], ["jdbc", 5432], ["mongodb", 27017],
  ].map(([source_type, port]) => ({ source_type, fields: databaseFields(port as number) })),
  {
    source_type: "elasticsearch", fields: [
      { key: "hostname", label: "Host", type: "string", required: true },
      { key: "port", label: "Port", type: "number", default: 9200 },
      { key: "scheme", label: "Scheme", type: "enum", default: "http", options: ["http", "https"] },
      { key: "username", label: "Username", type: "string" },
      { key: "password", label: "Password", type: "secret", persist: false },
    ],
  },
  ...["api", "rest", "webhook", "http"].map((source_type) => ({
    source_type, fields: [
      { key: "base_url", label: "Base URL", type: "string", required: true, placeholder: "https://api.example.com" },
      { key: "health_path", label: "Health check path", type: "string", placeholder: "/health" },
      { key: "endpoints", label: "Endpoints", type: "list", description: "Known endpoint names" },
    ],
  })),
  {
    source_type: "s3", fields: [
      { key: "bucket", label: "Bucket", type: "string", required: true },
      { key: "region", label: "Region", type: "string", default: "us-east-1" },
      { key: "access_key_id", label: "Access key ID", type: "secret", persist: false },
      { key: "secret_access_key", label: "Secret access key", type: "secret", persist: false },
      { key: "prefix", label: "Object key prefix", type: "string" },
    ],
  },
  {
    source_type: "gcs", fields: [
      { key: "bucket", label: "Bucket", type: "string", required: true },
      { key: "prefix", label: "Object key prefix", type: "string" },
      { key: "credentials_json", label: "Service account credentials (JSON)", type: "secret", persist: false },
    ],
  },
  {
    source_type: "azure_blob", fields: [
      { key: "account", label: "Storage account", type: "string", required: true },
      { key: "container", label: "Container", type: "string" },
    ],
  },
  {
    source_type: "adls_gen2", fields: [
      { key: "account_name", label: "Storage account name", type: "string", required: true },
      { key: "filesystem", label: "Filesystem (container) name", type: "string", required: true },
      { key: "client_id", label: "AAD application (client) ID", type: "string", required: true },
      { key: "client_secret", label: "AAD application client secret", type: "secret", required: true, persist: false },
      { key: "tenant_id", label: "AAD tenant ID", type: "string" },
    ],
  },
  {
    source_type: "iceberg", fields: [
      { key: "warehouse", label: "Warehouse location", type: "string", required: true, placeholder: "s3://bucket/warehouse" },
      { key: "catalog_name", label: "Catalog name", type: "string", default: "default" },
      { key: "catalog_type", label: "Catalog type", type: "enum", default: "hadoop", options: ["hadoop", "hive", "rest", "glue"] },
      { key: "catalog_uri", label: "Catalog URI (Hive/REST/Nessie)", type: "string" },
      { key: "namespace", label: "Table namespace", type: "string", default: "default" },
    ],
  },
  ...[["ftp", 21], ["sftp", 22]].map(([source_type, port]) => ({
    source_type, fields: [
      { key: "hostname", label: "Host", type: "string", required: true },
      { key: "port", label: "Port", type: "number", default: port },
      { key: "username", label: "Username", type: "string" },
      { key: "password", label: "Password", type: "secret", persist: false },
      { key: "path", label: "Remote path", type: "string", default: "." },
    ],
  })),
  {
    source_type: "file", fields: [
      { key: "path", label: "Path", type: "string", required: true },
      { key: "objects", label: "Objects", type: "list", description: "Known file names" },
    ],
  },
];

/**
 * The real backend submits deploy/pause/resume/restart/discover as an
 * Operation (202, polled via GET /operations/{id}) rather than completing
 * them inline — see src/api.ts's own header comment. This fixture has no
 * real async work to do, so it records the operation already "succeeded"
 * rather than simulating a delay: the frontend's poll loop (src/store/
 * pollOperation.ts) sees the terminal status on its first request either way.
 */
function createSucceededOperation(
  state: BackendState,
  operationType: string,
  resourceType: string,
  resourceId: string,
  result: unknown,
): Record<string, unknown> {
  const operation = {
    id: nextId("op"), org_id: "org_test", operation_type: operationType,
    resource_type: resourceType, resource_id: resourceId, status: "succeeded",
    result, error_message: null,
  };
  state.operations.push(operation);
  return operation;
}

export function freshState(): BackendState {
  return {
    connections: [],
    pipelines: [],
    transforms: [],
    qualityPolicies: [],
    qualityResults: [],
    schemas: [],
    driftApprovals: [],
    audit: [],
    operations: [],
    discoveryRuns: [],
    discoveryObjects: [],
    lineageAssets: [],
    lineageEdges: [],
    templates: [
      {
        id: "tpl-postgres-cdc",
        name: "Postgres CDC",
        connector_type: "source",
        category: "database",
        connector_class: "io.debezium.connector.postgresql.PostgresConnector",
        runtime_pool_type: "kafka-connect",
        config_schema: {},
        default_config: {},
        default_transforms: [],
        supported_transforms: [],
        version: "v1",
        is_active: true,
        // Mirrors the backend (app/schemas/catalog.py): Kafka Connect gives only SINK connectors a dead-letter
        // queue, so a source template always reports supportsDLQ: false.
        supports_dlq: false,
        capabilities: { cdc: true, snapshot: true, schemaDiscovery: true, supportsSMT: true, supportsDLQ: false },
      },
    ],
  };
}

function json(route: Route, status: number, body?: unknown) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: body === undefined ? "" : JSON.stringify(body),
  });
}

async function readBody(route: Route): Promise<Record<string, unknown>> {
  return (route.request().postDataJSON() as Record<string, unknown>) || {};
}

/** Installs the mock backend on `page` and returns the mutable state it serves from. */
const STATES = new WeakMap<Page, BackendState>();

export async function mockBackend(page: Page, state: BackendState = freshState()): Promise<BackendState> {
  STATES.set(page, state);
  // One handler for the whole /api/v1/dataconnect/** tree (health included —
  // it lives under the same prefix on the real backend) rather than a
  // separate page.route() per path, so there's no route-priority ordering to
  // get wrong between a "specific" and a "catch-all" registration.
  await page.route("**/api/v1/dataconnect/**", async (route) => {
    const req = route.request();
    const method = req.method();
    const path = new URL(req.url()).pathname.replace(/^.*\/api\/v1\/dataconnect/, "");

    if (path === "/health" && method === "GET") return json(route, 200, { status: "ok", service: "dataconnect" });
    if (path === "/me" && method === "GET") {
      const me = state.me ?? meFor("Admin");
      return typeof me === "number" ? json(route, me, { detail: "request did not come through the gateway" }) : json(route, 200, me);
    }

    // ── Connection config schemas (FE-BUG-10) ───────────────────────────────
    if (path === "/connection-config-schemas" && method === "GET") return json(route, 200, CONNECTION_CONFIG_SCHEMAS);

    // ── Connector templates ────────────────────────────────────────────────
    if (path === "/connector-templates" && method === "GET") return json(route, 200, state.templates);

    // ── Connections ──────────────────────────────────────────────────────
    if (path === "/connections" && method === "GET") return json(route, 200, state.connections);
    if (path === "/connections" && method === "POST") {
      const body = await readBody(route);
      const conn = { id: nextId("conn"), org_id: "org_test", status: "active", ...body };
      state.connections.push(conn);
      return json(route, 201, conn);
    }
    let m = path.match(/^\/connections\/([^/]+)$/);
    if (m && method === "GET") {
      const conn = state.connections.find((c) => c.id === m![1]);
      return conn ? json(route, 200, conn) : json(route, 404, { detail: "not found" });
    }
    if (m && method === "DELETE") {
      state.connections = state.connections.filter((c) => c.id !== m![1]);
      return route.fulfill({ status: 204, body: "" });
    }
    if (path === "/connections/test-connection" && method === "POST") return json(route, 200, { status: "ok", message: "Connected" });   // the form's probe of unsaved values
    m = path.match(/^\/connections\/([^/]+)\/test-connection$/);
    if (m && method === "POST") return json(route, 200, { status: "ok", message: "Connected" });
    m = path.match(/^\/connections\/([^/]+)\/discover$/);
    if (m && method === "POST") {
      // Real backend (GAP-10): 202 + a DiscoveryRun, not an Operation with
      // the objects inline — see app/api/routes/connections.py's
      // discover_connection. No real async work here either, so the run
      // (and whatever objects freshState/a test seeded for this connection
      // id) is already "succeeded" the moment it's created.
      const run = {
        id: nextId("run"), org_id: "org_test", connection_id: m[1], operation_id: nextId("op"),
        status: "succeeded", object_count: state.discoveryObjects.filter((o) => o.discovery_run_id === m![1]).length,
        continuation: null, error_message: null, truncated: Boolean(state.discoveryTruncated),
        created_at: new Date().toISOString(), completed_at: new Date().toISOString(),
      };
      // Objects seeded against the connection id (test convenience) get
      // re-tagged onto this run's own id so GET /discovery/{run_id}/objects
      // finds them.
      state.discoveryObjects
        .filter((o) => o.discovery_run_id === m![1])
        .forEach((o) => { o.discovery_run_id = run.id; });
      state.discoveryRuns.push(run);
      return json(route, 202, run);
    }
    m = path.match(/^\/discovery\/([^/]+)$/);
    if (m && method === "GET") {
      const run = state.discoveryRuns.find((r) => r.id === m![1]);
      return run ? json(route, 200, run) : json(route, 404, { detail: "not found" });
    }
    m = path.match(/^\/discovery\/([^/]+)\/facets$/);
    if (m && method === "GET") {
      const objects = state.discoveryObjects.filter((o) => o.discovery_run_id === m![1]);
      const schema_names = Array.from(new Set(objects.map((o) => o.schema_name).filter(Boolean) as string[])).sort();
      const object_types = Array.from(new Set(objects.map((o) => o.object_type).filter(Boolean) as string[])).sort();
      return json(route, 200, { schema_names, object_types });
    }
    m = path.match(/^\/discovery\/([^/]+)\/objects$/);
    if (m && method === "GET") {
      const url = new URL(req.url());
      const search = (url.searchParams.get("search") || "").toLowerCase();
      const objectType = url.searchParams.get("object_type");
      const schemaName = url.searchParams.get("schema_name");
      const limit = Number(url.searchParams.get("limit") || 100);
      const cursor = url.searchParams.get("cursor");
      let objects = state.discoveryObjects.filter((o) => o.discovery_run_id === m![1]);
      if (search) {
        objects = objects.filter((o) =>
          String(o.name || "").toLowerCase().includes(search)
          || String(o.schema_name || "").toLowerCase().includes(search));
      }
      if (objectType) objects = objects.filter((o) => o.object_type === objectType);
      if (schemaName) objects = objects.filter((o) => o.schema_name === schemaName);
      objects = objects.slice().sort((a, b) => String(a.id).localeCompare(String(b.id)));
      if (cursor) objects = objects.filter((o) => String(o.id) > cursor);
      const page = objects.slice(0, limit);
      const next_cursor = objects.length > limit ? String(page[page.length - 1].id) : null;
      return json(route, 200, { objects: page, next_cursor });
    }

    // ── Pipelines ────────────────────────────────────────────────────────
    if (path === "/pipelines" && method === "GET") return json(route, 200, state.pipelines);
    if (path === "/pipelines" && method === "POST") {
      const body = await readBody(route);
      const pipe = {
        id: nextId("pipe"), org_id: "org_test", status: "draft",
        topic_name: `org_test.${body.domain}.${body.source_system}.${body.entity}`,
        connector_name: `${body.name}-connector`, generated_config: {}, revision: 1, ...body,
      };
      state.pipelines.push(pipe);
      return json(route, 201, pipe);
    }
    // FE-BUG-08: mirrors app/services/topic_naming.py's build_topic_name — seven fields joined with "-" ONLY (no "."), each of
    // domain/source_system/entity restricted to [a-z0-9_] because "-" is the separator, and a version like v1 (BUG-01) —
    // registered before the generic single-pipeline GET match below, since
    // "/pipelines/topic-name-preview" would otherwise be swallowed by that
    // route's `/pipelines/{id}` pattern (and 404, "topic-name-preview" not
    // being a real pipeline id).
    if (path === "/pipelines/topic-name-preview" && method === "GET") {
      const url = new URL(req.url());
      const q = (key: string) => url.searchParams.get(key) ?? "";
      for (const field of ["domain", "source_system", "entity"]) {
        const value = q(field);
        if (!/^[a-z0-9_]+$/.test(value)) {
          return json(route, 400, {
            code: "bad_request", retryable: false, field_errors: null,
            message: `${field} must be lowercase and contain only a-z, 0-9 or _ ('-' is the separator between the parts of a topic name, so it cannot appear inside one; use '_' instead, e.g. '${value.toLowerCase().replace(/[^a-z0-9_]+/g, "_")}')`,
          });
        }
      }
      if (!/^v[0-9]+$/.test(q("version"))) return json(route, 400, { code: "bad_request", retryable: false, field_errors: null, message: "version must match ^v[0-9]+$" });
      const topic_name = ["org_test", q("zone"), q("domain"), q("source_system"), q("entity"), q("mode"), q("version")].join("-");
      return json(route, 200, { topic_name });
    }
    m = path.match(/^\/pipelines\/([^/]+)$/);
    if (m && method === "GET") {
      const pipe = state.pipelines.find((p) => p.id === m![1]);
      return pipe ? json(route, 200, pipe) : json(route, 404, { detail: "not found" });
    }
    if (m && method === "PATCH") {
      // FE-BUG-14: mirrors app/services/pipeline_service.py's update_pipeline
      // — expected_revision is opt-in (only checked when the caller sends
      // it), a mismatch is a 409 PIPELINE_VERSION_CONFLICT carrying the
      // pipeline's current state in details.current, and an applied change
      // bumps revision by exactly 1.
      const pipe = state.pipelines.find((p) => p.id === m![1]);
      if (!pipe) return json(route, 404, { detail: "not found" });
      const body = await readBody(route);
      const currentRevision = (pipe.revision as number | undefined) ?? 1;
      const { expected_revision, ...fields } = body;
      if (expected_revision != null && expected_revision !== currentRevision) {
        return json(route, 409, {
          code: "PIPELINE_VERSION_CONFLICT",
          message: "This pipeline changed since you opened it.",
          retryable: false,
          details: { current: pipe },
        });
      }
      Object.assign(pipe, fields);
      pipe.revision = currentRevision + 1;
      return json(route, 200, pipe);
    }
    if (m && method === "DELETE") {
      state.pipelines = state.pipelines.filter((p) => p.id !== m![1]);
      return route.fulfill({ status: 204, body: "" });
    }
    // FE-BUG-24: "Ready to deploy" review screen — defaults to every check
    // passing (matching this whole fixture's "unrestricted unless a test
    // sets otherwise" convention, e.g. freshState's template). A test that
    // wants a failed check overrides this route itself via page.route(),
    // registered after mockBackend() so it runs first (see fixtures.ts's
    // module header comment on route-handler ordering).
    m = path.match(/^\/pipelines\/([^/]+)\/deploy-readiness$/);
    if (m && method === "GET") {
      const pipe = state.pipelines.find((p) => p.id === m![1]);
      if (!pipe) return json(route, 404, { detail: "not found" });
      const checks = [
        ["connection_verified", "Connection verified"],
        ["connector_plugin_installed", "Connector plugin installed"],
        ["connector_compatibility", "Connector compatibility (SMTs, converters, plugin version, mode)"],
        ["source_table_exists", "Source table exists"],
        ["topic_policy_valid", "Topic policy valid"],
        ["topic_name_available", "Topic name available"],
        ["schema_valid", "Schema valid"],
        ["transform_configuration_valid", "Transform configuration valid"],
        ["quality_policy_valid", "Quality check valid"],
        ["secret_reference_resolved", "Secret reference resolved"],
      ].map(([key, label]) => ({ key, label, ok: true, reason: null }));
      return json(route, 200, { pipeline_id: m[1], topic_name: pipe.topic_name ?? null, ready: true, checks });
    }
    for (const action of ["deploy", "pause", "resume", "restart"]) {
      m = path.match(new RegExp(`^/pipelines/([^/]+)/${action}$`));
      if (m && method === "POST") {
        const pipe = state.pipelines.find((p) => p.id === m![1]);
        const status = action === "deploy" ? "running" : action === "pause" ? "paused" : "running";
        if (pipe) pipe.status = status;
        const result = {
          pipeline_id: m[1], status, connector_name: pipe?.connector_name ?? "conn",
          topic_name: pipe?.topic_name ?? "topic", runtime: {},
        };
        const operation = createSucceededOperation(state, `pipeline_${action}`, "pipeline", m[1], result);
        return json(route, 202, operation);
      }
    }

    // ── Operations (deploy/pause/resume/restart/discover polling) ────────
    m = path.match(/^\/operations\/([^/]+)$/);
    if (m && method === "GET") {
      const operation = state.operations.find((o) => o.id === m![1]);
      return operation ? json(route, 200, operation) : json(route, 404, { detail: "not found" });
    }

    // ── Transforms ───────────────────────────────────────────────────────
    if (path === "/transform-policies" && method === "GET") return json(route, 200, state.transforms);
    const tfOne = path.match(/^\/transform-policies\/([^/]+)$/);
    if (tfOne && method === "DELETE") {
      state.transforms = state.transforms.filter((t) => t.id !== tfOne[1]);
      return route.fulfill({ status: 204, body: "" });
    }
    if (path === "/transform-policies" && method === "POST") {
      const body = await readBody(route);
      const tf = { id: nextId("tf"), org_id: "org_test", ...body };
      state.transforms.push(tf);
      return json(route, 201, tf);
    }

    // ── Schemas / drift ──────────────────────────────────────────────────
    if (path === "/schemas" && method === "GET") return json(route, 200, state.schemas);
    if (path === "/schemas/drift-approvals" && method === "GET") return json(route, 200, state.driftApprovals);
    for (const decision of ["approve", "reject"]) {
      m = path.match(new RegExp(`^/schemas/drift-approvals/([^/]+)/${decision}$`));
      if (m && method === "POST") {
        const body = await readBody(route);
        const approval = state.driftApprovals.find((a) => a.id === m![1]);
        if (approval) {
          approval.status = decision === "approve" ? "approved" : "rejected";
          approval.reviewed_by = body.reviewed_by;
        }
        return approval ? json(route, 200, approval) : json(route, 404, { detail: "not found" });
      }
    }

    // ── Quality ──────────────────────────────────────────────────────────
    if (path === "/quality-policies/capabilities" && method === "GET") {
      if (state.qualityCapabilities === "fail") return json(route, 500, { code: "INTERNAL_ERROR", message: "boom", retryable: false });
      return json(route, 200, state.qualityCapabilities ?? QUALITY_CAPABILITIES);
    }
    if (path === "/quality-policies" && method === "GET") return json(route, 200, state.qualityPolicies);
    if (path === "/quality-policies" && method === "POST") {
      const body = await readBody(route);
      const policy = { id: nextId("qp"), org_id: "org_test", ...body };
      state.qualityPolicies.push(policy);
      return json(route, 201, policy);
    }
    if (path === "/quality-results" && method === "GET") return json(route, 200, state.qualityResults);

    // ── Lineage / monitoring / audit ─────────────────────────────────────
    if (path === "/assets" && method === "GET") return json(route, 200, state.lineageAssets);
    m = path.match(/^\/lineage\/pipeline\/([^/]+)$/);
    if (m && method === "GET") {
      const edges = state.lineageEdges.filter((e) => e.pipeline_id === m![1]);
      const ids = new Set(edges.flatMap((e) => [e.from_asset_id, e.to_asset_id]));
      const nodes = state.lineageAssets.filter((a) => ids.has(a.id));
      return json(route, 200, { nodes, edges });
    }
    m = path.match(/^\/lineage\/([^/]+)$/);
    if (m && method === "GET") {
      // FE-BUG-20: mirrors app/services/lineage_service.py's graph_for_asset
      // bounded BFS exactly (frontier/hop/direction/asset_type semantics),
      // so the expand-upstream/expand-downstream interaction is exercised
      // against realistic paginated/bounded behavior, not a stub.
      const url = new URL(req.url());
      const depth = Number(url.searchParams.get("depth") ?? 1);
      const direction = url.searchParams.get("direction") || "both";
      const assetType = url.searchParams.get("asset_type") || undefined;
      const limit = Number(url.searchParams.get("limit") ?? 100);
      const offset = Number(url.searchParams.get("offset") ?? 0);
      const seed = state.lineageAssets.find((a) => a.id === m![1]);
      if (!seed) return json(route, 200, { nodes: [], edges: [], truncated: false, omitted_edges: 0, next_offset: null });
      const nodesById = new Map<string, Record<string, unknown>>([[seed.id as string, seed]]);
      const edgesById = new Map<string, Record<string, unknown>>();
      let frontier = new Set<string>([seed.id as string]);
      let omitted = 0;
      let nextOffset: number | null = null;
      for (let hop = 0; hop < depth && frontier.size; hop += 1) {
        // Direction applied first, stable id order, then the width bound (limit/offset).
        const touching = state.lineageEdges
          .filter((e) => {
            const leaves = frontier.has(e.from_asset_id as string);
            const arrives = frontier.has(e.to_asset_id as string);
            return direction === "downstream" ? leaves : direction === "upstream" ? arrives : leaves || arrives;
          })
          .sort((a, b) => String(a.id).localeCompare(String(b.id)));
        const hopOffset = hop === 0 ? offset : 0;
        const page = touching.slice(hopOffset, hopOffset + limit);
        const left = Math.max(0, touching.length - hopOffset - limit);
        if (left > 0) {
          omitted += left;
          if (hop === 0) nextOffset = hopOffset + limit;
        }
        const hopEdges = page;
        const neighborIds = new Set<string>();
        for (const e of hopEdges) {
          if (frontier.has(e.from_asset_id as string) && direction !== "upstream") neighborIds.add(e.to_asset_id as string);
          if (frontier.has(e.to_asset_id as string) && direction !== "downstream") neighborIds.add(e.from_asset_id as string);
        }
        const nextFrontier = new Set<string>();
        for (const id of neighborIds) {
          if (nodesById.has(id)) continue;
          const a = state.lineageAssets.find((x) => x.id === id);
          if (!a) continue;
          if (assetType && a.asset_type !== assetType) continue;
          nodesById.set(id, a);
          nextFrontier.add(id);
        }
        for (const e of hopEdges) {
          if (nodesById.has(e.from_asset_id as string) && nodesById.has(e.to_asset_id as string)) {
            edgesById.set(e.id as string, e);
          }
        }
        frontier = nextFrontier;
      }
      return json(route, 200, {
        nodes: [...nodesById.values()], edges: [...edgesById.values()],
        truncated: omitted > 0, omitted_edges: omitted, next_offset: nextOffset,
      });
    }
    if (path === "/runtime/flow" && method === "GET") {
      // FE-BUG-16: real shape, reachable:true by default — mirrors
      // app/schemas/monitoring.py's RuntimeFlow (previously untyped
      // passthrough JSON with no `reachable`/`message` a test could even
      // model). Individual tests override via page.route for the
      // unreachable/stale scenarios.
      // A deployed pipeline is a live connector in Kafka Connect, as in the real
      // system — the Pipelines list only trusts live Connect state (not the
      // database status) for a deployed pipeline's badge.
      const connectors = state.pipelines
        .filter((p) => p.connector_name && (p.status === "running" || p.status === "paused"))
        .map((p) => ({
          name: p.connector_name, pipeline_id: p.id, type: "source",
          state: p.status === "paused" ? "PAUSED" : "RUNNING", tasks: [p.status === "paused" ? "PAUSED" : "RUNNING"], topics: [p.topic_name],
        }));
      return json(route, 200, { reachable: true, connect_url: "", bootstrap: "", connectors, topics: [], sinks: [], message: "" });
    }
    m = path.match(/^\/runtime\/connectors\/([^/]+)\/status$/);
    if (m && method === "GET") {
      return json(route, 200, {
        name: m[1], pipeline_id: "unknown", connector: {}, tasks: [],
        observed_state: "unknown", observed_at: new Date().toISOString(), runtime_endpoint: null, error: null,
      });
    }
    m = path.match(/^\/runtime\/connectors\/([^/]+)\/metrics$/);
    if (m && method === "GET") {
      // Real ConnectorMetrics shape (app/schemas/monitoring.py). Prometheus "not configured" by default —
      // tests of the populated view override via page.route.
      const name = decodeURIComponent(m[1]!);
      const pipe = state.pipelines.find((p) => p.connector_name === name);
      if (!pipe) return json(route, 404, { detail: `Connector '${name}' not found` });
      return json(route, 200, {
        connector_name: name, pipeline_id: pipe.id, pipeline_name: pipe.name, direction: "source", runtime_pool_id: null,
        range: new URL(req.url()).searchParams.get("range") || "1h", step_seconds: 60, observed_at: new Date().toISOString(),
        prometheus_enabled: false, reachable: false,
        error: "Prometheus is not configured for this deployment (DATACONNECT_PROMETHEUS_URL).",
        metrics: [], task_columns: [], tasks: [],
      });
    }
    if (path === "/runtime/connectors/metrics/summary" && method === "GET") {
      // Real ConnectorMetricsSummary shape (app/schemas/monitoring.py): Prometheus "not configured" by default, so
      // every rate/lag/task count is null (a dash in the UI) — tests of the populated view override via page.route.
      return json(route, 200, {
        prometheus_enabled: false, reachable: false,
        error: "Prometheus is not configured for this deployment (DATACONNECT_PROMETHEUS_URL).",
        observed_at: new Date().toISOString(),
        connectors: state.pipelines.filter((p) => p.connector_name).map((p) => ({
          connector_name: p.connector_name, pipeline_id: p.id, pipeline_name: p.name,
          direction: String(p.direction ?? "").toUpperCase() === "SINK" ? "sink" : "source", runtime_pool_id: null,
          rate: null, lag: null, tasks_running: null, tasks_failed: null,
        })),
      });
    }
    if (path === "/runtime/connect-clusters" && method === "GET") return json(route, 200, []);
    if (path === "/runtime/metrics/failures" && method === "GET") {
      // FE-BUG-17: real FailureBreakdown shape (app/schemas/monitoring.py), empty by default.
      return json(route, 200, {
        window_seconds: 3600,
        validation: { total: 0, by_rule: [], by_check: [] },
        ingestion: { total: 0, classified: 0, unclassified: 0, by_category: [], recent: [] },
      });
    }
    if (path === "/runtime/metrics/summary" && method === "GET") {
      // FE-BUG-17: real MetricsSummary shape (app/schemas/monitoring.py) —
      // validation_failure_count/ingestion_failure_count replaced the old
      // single, always-0 quarantine_count. Individual tests override via
      // page.route for non-zero scenarios.
      return json(route, 200, {
        connector_running_count: 0, failed_connector_count: 0, failed_task_count: 0,
        records_read_per_second: 0, records_written_per_second: 0, error_rate: 0,
        validation_failure_count: 0, ingestion_failure_count: 0, consumer_lag: 0, extra: {},
      });
    }
    if (path === "/audit" && method === "GET") return json(route, 200, state.audit);

    // Anything unhandled: an empty collection for reads, an accepted no-op for writes —
    // keeps specs that don't care about a given side-call from failing on a 404.
    if (method === "GET") return json(route, 200, []);
    return json(route, 200, {});
  });

  return state;
}

/**
 * FE-BUG-24: clicking a row's Deploy icon now opens the "Ready to deploy"
 * review screen instead of firing the deploy action immediately — this
 * clicks through it (waiting for the readiness fetch, which the default
 * mockBackend route always resolves ready:true, to settle) the same way
 * every existing deploy-lifecycle spec used to click the icon directly.
 */
export async function clickDeploy(page: Page, row: Locator) {
  await rowAction(row, "Deploy");
  const dialog = page.locator(".modal");
  await expect(dialog).toContainText("Ready to deploy");
  const deployButton = dialog.getByRole("button", { name: "Deploy", exact: true });
  await expect(deployButton).toBeEnabled();
  await deployButton.click();
}

/**
 * Forces the next matching mock backend call to fail — for error-state
 * specs. Falls through to the broader mockBackend() route (registered
 * earlier, so lower priority) for every other request, including later
 * calls to the same path — this only ever fails once.
 */
export async function mockNextCallFailure(
  page: Page,
  pathSuffix: string,
  status = 500,
  detail = "boom",
  method?: string,
) {
  let used = false;
  await page.route(`**${pathSuffix}`, (route) => {
    if (used || (method && route.request().method() !== method)) return route.fallback();
    used = true;
    return json(route, status, { detail });
  });
}

/** A SINK template as the backend serves it: the only kind of connector with a dead-letter queue. */
export const SINK_TEMPLATE = {
  id: "tpl-jdbc-sink",
  name: "JDBC Sink",
  connector_type: "sink",
  category: "database",
  connector_class: "io.confluent.connect.jdbc.JdbcSinkConnector",
  runtime_pool_type: "kafka-connect",
  config_schema: {},
  default_config: {},
  default_transforms: [],
  supported_transforms: [],
  version: "v1",
  is_active: true,
  supports_dlq: true,
  capabilities: { cdc: true, snapshot: true, schemaDiscovery: true, supportsSMT: true, supportsDLQ: true },
};


// ── Acting as a persona: GET /me answers with that persona's permissions; the app reads /me once per load, so reload ──
/** Sign in again as `role` (a test persona): the mock /me now answers with its permissions, and the page reloads onto the same page. */
export async function switchRole(page: Page, role: RoleName) {
  const state = STATES.get(page);
  if (!state) throw new Error("switchRole needs mockBackend(page) first");
  const current = typeof state.me === "object" ? state.me : meFor("Admin");
  state.me = { ...current, permissions: [...PERSONAS[role]] };
  await page.reload();
  await expectActiveRole(page, role);
}

/** Sign in again into another tenant (GET /me reports it); takes effect at the next page load. */
export function setTenant(page: Page, tenant: string) {
  const state = STATES.get(page);
  if (!state) throw new Error("setTenant needs mockBackend(page) first");
  state.me = { ...(typeof state.me === "object" ? state.me : meFor("Admin")), tenant };
}

/** The app has loaded /me and holds exactly `role`'s permissions. */
export async function expectActiveRole(page: Page, role: RoleName) {
  await expect(page.locator(".app")).toHaveAttribute("data-dc-access", "ready");
  const state = STATES.get(page);
  if (state && typeof state.me === "object") expect(state.me.permissions).toEqual(PERSONAS[role]);
}

/**
 * Pick an option of one of DataConnect's dropdowns (components/Select.tsx, the Catalog's SearchSelect) the way a person does: open it, click the
 * row. `pick` is the option's value, or `{ index }` for its place in the list; the trigger says which label that value has (data-options).
 */
export async function choose(trigger: Locator, pick: string | { index: number }) {
  const read = async () => JSON.parse((await trigger.getAttribute("data-options")) ?? "[]") as { value: string; label: string }[];
  const find = (list: { value: string; label: string }[]) => (typeof pick === "string" ? list.find((o) => o.value === pick) : list[pick.index]);
  // The options of a list that is still loading (object types found by discovery, say) arrive a moment after the dropdown is on screen.
  await expect.poll(async () => find(await read()) !== undefined, { message: `no option ${JSON.stringify(pick)} in the dropdown` }).toBe(true);
  const options = await read();
  const option = find(options)!;
  if ((await trigger.getAttribute("aria-expanded")) !== "true") await trigger.click();
  const rows = trigger.page().locator(".ss-menu [role=option]");
  const same = options.filter((o) => o.label === option.label);
  await (same.length > 1 ? rows.nth(options.indexOf(option)) : rows.filter({ hasText: new RegExp(`^\\s*${option.label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*$`) }).first()).click();
}

/** Open a DataConnect dropdown and give its rows: `rows` (all of them, in order) and `row(value)`. Close it again with Escape. */
export async function openList(trigger: Locator) {
  await expect.poll(async () => JSON.parse((await trigger.getAttribute("data-options")) ?? "[]").length).toBeGreaterThan(0);   // not before the options have arrived
  const options = JSON.parse((await trigger.getAttribute("data-options")) ?? "[]") as { value: string; label: string }[];
  if ((await trigger.getAttribute("aria-expanded")) !== "true") await trigger.click();
  const rows = trigger.page().locator(".ss-menu [role=option]");
  await rows.first().waitFor();
  return { rows, row: (value: string) => rows.nth(options.findIndex((o) => o.value === value)) };
}

/** The rows of a DataConnect dropdown, opened if it is not already (a test reads the list the way a person does). */
export async function rowsOf(trigger: Locator) { return (await openList(trigger)).rows; }

// ── A row's "three dots" actions menu (components/RowMenu.tsx) ────────────────────────────────────────────────────────────────

/** The names the menu's lines go by, written as the old icon buttons' titles were (the accessible names carry the row's name: "Deploy orders-cdc"). */
const MENU_ITEM = {
  "Deploy": /^Deploy /, "Pause": /^Pause /, "Resume": /^Resume /, "Restart": /^Restart /, "Edit": /^Edit /, "Delete": /^Delete /,
  "View pipeline detail": /^View .* detail$/, "View metrics": /^View metrics for /,
} as const;
export type RowAction = keyof typeof MENU_ITEM;

/** The row's three-dots button. */
export const rowMenuButton = (row: Locator) => row.getByRole("button", { name: /^Actions for / });

/** Open the row's menu (if it is not open) and return one of its lines: a RowAction, or an exact accessible name. */
export async function menuItem(row: Locator, action: RowAction | string): Promise<Locator> {
  const button = rowMenuButton(row);
  if ((await button.getAttribute("aria-expanded")) !== "true") await button.click();
  return action in MENU_ITEM
    ? row.page().getByRole("menuitem", { name: MENU_ITEM[action as RowAction] })
    : row.page().getByRole("menuitem", { name: action, exact: true });      // an exact accessible name: "View x" must not also match "Preview x"
}

/** Do what a person does: open the row's menu, click the line. */
export async function rowAction(row: Locator, action: RowAction | string) {
  await (await menuItem(row, action)).click();
}
