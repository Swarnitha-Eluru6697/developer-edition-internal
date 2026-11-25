/*
 * DataConnect API client — talks to the FastAPI backend.
 *
 * Ported from the vanilla api.js, but turned into a factory so the host can
 * inject where the backend lives. In the federated (production) path the host
 * passes `baseUrl` = the tunnel route
 *     /control_plane/api/dataplane/<dpId>/api/v1/dataconnect
 * which is same-origin + relative, so the session cookie rides and the BFF
 * mints the data-plane ticket. Standalone dev falls back to the local backend.
 *
 * Every write is reported to the host via onOperation() so the Control Portal
 * can surface it in its own activity bell. There is no postMessage/iframe
 * fallback here (there used to be, targeting "*" as the parent origin, from
 * a pre-Module-Federation DC_OPERATION contract) — this is loaded via
 * Module Federation as a real remote sharing the host's own window and React
 * runtime (see DataConnectApp.tsx), never inside an iframe, so
 * `window.parent !== window` never holds for it and onOperation() is the
 * host's only, complete signal. If a genuinely iframe-embedded consumer of
 * this client is ever needed again, that's new, origin-validated code to
 * write (an exact expected-parent-origin config, not a "*" target), not a
 * fallback to resurrect.
 *
 * Every method's request/response is typed against src/types/api.ts, which is
 * derived from the backend's own OpenAPI schema (`npm run gen:api-types`) —
 * a renamed or reshaped backend field now fails `npm run typecheck` here
 * instead of surfacing at runtime.
 *
 * Cancellation: every method takes an optional trailing `signal` — the same
 * AbortSignal RTK Query hands its queryFn (see store/apiSlice.ts's `call()`),
 * which it aborts automatically when a component unmounts mid-request or a
 * query's args change before the previous one resolved (a search box typing
 * ahead of its own last request, a poll firing again before the last tick
 * answered, a page navigated away from). Passing it straight to `fetch()` is
 * what turns "RTK Query stopped caring about this" into "the browser stops
 * the request in flight" instead of a page transition merely ignoring a
 * response that's still burning bandwidth (and, for a slow backend, still
 * doing real work) in the background.
 *
 * Retries: a transient upstream failure (502/503/504, or the fetch itself
 * throwing — DNS/connection-refused/etc.) gets up to 2 retries with
 * full-jitter exponential backoff (see withRetry() below), but only for
 * idempotent methods — GET, PUT, DELETE. POST and PATCH get exactly one
 * attempt: idempotency, not "is this a read", is what makes a retry safe
 * (repeating an idempotent request has the same end effect whether or not
 * the first attempt's response ever arrived), and neither a POST-created
 * resource nor this app's arbitrary-partial-field PATCH updates carry that
 * guarantee. A 4xx or a plain 500 is never retried regardless of method —
 * that's the request being rejected or failing outright, not the backend
 * being transiently unreachable.
 */
import type {
  AlertChannel, AlertChannelCreate, AlertChannelPatch, AlertChannelTestResult, AlertCounts, AlertDetail, AlertRule, AlertRuleCreate,
  AlertRuleKindInfo, AlertRulePatch, AlertsPage, AlertState, AlertSweepResult,
  Asset, AuditLogEntry, ConnectCluster, Connection, ConnectionConfigSchema, ConnectionCreate, ConnectionTestResult,
  ConnectorMetricRange, ConnectorMetrics, ConnectorMetricsSummary, ConnectorStatus, ConnectorTask, ConnectorTemplate, ConnectorTemplateCreate,
  DiscoveryFacets, DiscoveryObjectPage, DiscoveryRun,
  GeneratedPipelineConfig, HealthResponse, LineageGraph, LineageRuntimeEvent,
  DeployReadiness, FailureBreakdown, MetricsSummary, Operation, Pipeline, PipelineCreate, PipelineStatus, PipelineUpdate,
  QualityCapabilities, QualityExecuteResult, QualityExecutionRequest, QualityPolicy, QualityPolicyCreate, QualityResult, QualityStreamStats,
  RuntimeConnector, RuntimeFlow, SchemaDriftApproval, SchemaDriftEvent,
  SchemaDriftResult, SchemaDriftReviewRequest, SchemaInferResult, SchemaVersion,
  SchemaVersionCreate, StreamingQualityRequest, StreamingQualityResult, TransformPolicy,
  TransformPolicyCreate, TransformPreviewResult, TopicNamePreview, ValidationResult,
} from "./types/api";
import type { Me } from "./permissions";

export interface ApiOperation {
  status: "success" | "error";
  resource?: string;
  action?: string;
  id?: string;
  name?: string;
  message?: string;
  httpStatus?: number;
}

export interface ApiConfig {
  baseUrl?: string;
  /* No identity, tenant or role is sent from here: Envoy sets the user, tenant and permissions from the signed ticket,
   * and the backend ignores any such header a client sends (contract docs/auth/01-CONTRACT.md section 4). */
  onOperation?: (op: ApiOperation) => void;
}

interface RequestOptions {
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  signal?: AbortSignal;
}

/** One field-level validation failure — mirrors the backend's app/schemas/errors.py FieldError. */
export interface ApiFieldError {
  field: string;
  message: string;
}

export interface ApiErrorOptions {
  /** Stable, machine-readable identifier — see the backend's app/core/errors.py DataConnectError
   *  subclasses for the fixed set it raises deliberately, e.g. "connector_config_error",
   *  "kafka_connect_error", "backend_unavailable". Undefined when the response predates the
   *  standard envelope (a bare string body, or no body at all — e.g. this client's own network-
   *  level failures, which never reached a backend response to carry a code at all). */
  code?: string;
  fieldErrors?: ApiFieldError[];
  requestId?: string;
  /** Whether retrying the exact same request might succeed unchanged — the backend's own
   *  judgment (see ErrorEnvelope.retryable), not re-derived from the status code here. Defaults
   *  to false: treating an error as safe to retry needs the backend to say so explicitly. */
  retryable: boolean;
  details?: Record<string, unknown>;
}

export class ApiError extends Error {
  status: number;
  code?: string;
  fieldErrors?: ApiFieldError[];
  requestId?: string;
  retryable: boolean;
  details?: Record<string, unknown>;

  constructor(status: number, message: string, options?: Partial<ApiErrorOptions>) {
    super(message);
    this.status = status;
    this.code = options?.code;
    this.fieldErrors = options?.fieldErrors;
    this.requestId = options?.requestId;
    this.retryable = options?.retryable ?? false;
    this.details = options?.details;
  }
}

/** The backend's standard error envelope shape (app/schemas/errors.py's ErrorEnvelope) — a raw,
 *  unvalidated JSON body is narrowed to this via isErrorEnvelope() before being trusted. */
interface ErrorEnvelopeBody {
  code: string;
  message: string;
  field_errors: ApiFieldError[] | null;
  request_id: string | null;
  retryable: boolean;
  details: Record<string, unknown> | null;
}

function isErrorEnvelope(data: unknown): data is ErrorEnvelopeBody {
  return (
    typeof data === "object" && data !== null &&
    typeof (data as { code?: unknown }).code === "string" &&
    typeof (data as { message?: unknown }).message === "string" &&
    typeof (data as { retryable?: unknown }).retryable === "boolean"
  );
}

function isAbortError(e: unknown): boolean {
  return e instanceof Error && e.name === "AbortError";
}

// ── Retry / backoff ──────────────────────────────────────────────────────
// Idempotency, not "is this a GET", is what makes a retry safe: repeating
// GET/PUT/DELETE has the same end effect whether or not the first attempt's
// response ever reached us, so retrying one after a transient failure can't
// duplicate a side effect. POST and PATCH have no such guarantee here (POST
// creates a new resource every time it actually lands; this app's PATCH
// updates are arbitrary partial field sets, not a format the HTTP spec
// promises is idempotent) — those get exactly one attempt, full stop.
function isIdempotentMethod(method: string): boolean {
  return method === "GET" || method === "PUT" || method === "DELETE";
}

// Gateway/upstream failures worth retrying — the backend (or something in
// front of it) is transiently unavailable, not rejecting the request itself.
// 4xx and a plain 500 are left alone: those mean the request was received
// and something about it (or unrelated application logic) failed, and
// blindly repeating it won't change that.
const RETRYABLE_STATUS = new Set([502, 503, 504]);

const MAX_RETRY_ATTEMPTS = 2; // up to 2 retries beyond the first try = 3 attempts total
const RETRY_BASE_DELAY_MS = 250;
const RETRY_MAX_DELAY_MS = 2000;

/** Full-jitter exponential backoff: a random delay in [0, cap], cap doubling each attempt — spreads
 *  retries out so every client hitting the same transient outage doesn't all hammer it again in sync. */
function backoffDelay(attempt: number): number {
  const cap = Math.min(RETRY_MAX_DELAY_MS, RETRY_BASE_DELAY_MS * 2 ** attempt);
  return Math.random() * cap;
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new DOMException("The operation was aborted.", "AbortError")); return; }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(new DOMException("The operation was aborted.", "AbortError"));
    }, { once: true });
  });
}

/**
 * Runs `attempt()` (one fetch), retrying on a transient failure only when
 * `retryable` (i.e. the HTTP method is idempotent) — a caller-triggered
 * cancellation always propagates immediately, never counts as something to
 * retry past.
 */
async function withRetry(retryable: boolean, signal: AbortSignal | undefined, attempt: () => Promise<Response>): Promise<Response> {
  let tries = 0;
  for (;;) {
    try {
      const res = await attempt();
      if (!retryable || !RETRYABLE_STATUS.has(res.status) || tries >= MAX_RETRY_ATTEMPTS) return res;
      tries += 1;
      await sleep(backoffDelay(tries), signal);
    } catch (e) {
      if (isAbortError(e) || !retryable || tries >= MAX_RETRY_ATTEMPTS) throw e;
      tries += 1;
      await sleep(backoffDelay(tries), signal);
    }
  }
}

function uuid(): string {
  return (crypto.randomUUID && crypto.randomUUID()) ||
    "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c: string) => {
      const r = (Math.random() * 16) | 0, v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
}

interface DescribedWrite { resource: string; action: string; id?: string }

/** "/pipelines/42/deploy" + POST → a readable resource + action pair. */
function describe(method: string, path: string): DescribedWrite {
  const segments = (path.split("?")[0] || "").split("/").filter(Boolean);
  const resource = (segments[0] || "resource").replace(/-/g, " ");
  const tail = segments[segments.length - 1] || "";
  const isVerb = segments.length > 2 && !/^[0-9a-f-]{6,}$/i.test(tail);
  const action = isVerb
    ? tail
    : ({ POST: "created", PUT: "updated", PATCH: "updated", DELETE: "deleted" }[method] || "changed");
  const id = isVerb ? segments[segments.length - 2] : segments[1];
  return { resource, action, id: id && /^[0-9a-z-]{4,}$/i.test(id) ? id : undefined };
}

/** Loose shape write responses are inspected for, to feed the telemetry report — payloads vary per domain. */
interface WriteResponseHint {
  id?: string;
  pipeline_id?: string;
  connection_id?: string;
  name?: string;
  pipeline_name?: string;
}

export function createApi({ baseUrl, onOperation }: ApiConfig) {
  const base = String(baseUrl || "").replace(/\/$/, "");

  function report(detail: ApiOperation): void {
    try { onOperation && onOperation(detail); } catch { /* never let telemetry break a request */ }
  }

  async function request<T>(method: string, path: string, { body, query, signal }: RequestOptions = {}): Promise<T> {
    let url = base + path;
    if (query) {
      const qs = Object.entries(query).filter(([, v]) => v != null && v !== "")
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`).join("&");
      if (qs) url += "?" + qs;
    }
    const headers: Record<string, string> = { "X-Request-Id": uuid() };
    if (body !== undefined) headers["Content-Type"] = "application/json";

    const isWrite = method !== "GET";
    const described = isWrite ? describe(method, path) : undefined;
    const retryable = isIdempotentMethod(method);

    let res: Response;
    try {
      res = await withRetry(retryable, signal, () => fetch(url, {
        method,
        headers,
        credentials: "include", // session cookie must ride the same-origin tunnel path
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal,
      }));
    } catch (e) {
      // A deliberate cancellation (component unmounted, a newer request
      // superseded this one) is not a network failure — don't wrap it as one,
      // and don't report() it as a failed write; the caller stopped caring,
      // the backend didn't do anything wrong.
      if (isAbortError(e)) throw e;
      const message = `Network error — is the backend reachable at ${base}?`;
      if (isWrite && described) report({ status: "error", resource: described.resource, action: described.action, id: described.id, message });
      // Never reached the backend at all, so there's no envelope's own
      // `retryable` to defer to — but a request that couldn't even complete
      // is exactly the shape of failure worth offering a retry for.
      throw new ApiError(0, message, { retryable: true });
    }
    const text = await res.text();
    let data: unknown = null;
    if (text) { try { data = JSON.parse(text); } catch { data = text; } }
    if (!res.ok) {
      let apiError: ApiError;
      if (isErrorEnvelope(data)) {
        apiError = new ApiError(res.status, data.message, {
          code: data.code,
          fieldErrors: data.field_errors ?? undefined,
          requestId: data.request_id ?? undefined,
          retryable: data.retryable,
          details: data.details ?? undefined,
        });
      } else {
        // Predates the standard envelope, or came from something other than
        // this backend entirely (a proxy's own error page, say) — fall back
        // to the old bare-string `detail` shape rather than losing the
        // message. No code/fieldErrors/requestId to offer here; retryable
        // stays the ApiError default (false) since nothing said otherwise.
        const errBody = data as { detail?: unknown } | string | null;
        const detail: unknown = errBody && typeof errBody === "object" && errBody.detail !== undefined ? errBody.detail : res.statusText;
        const message = typeof detail === "string" ? detail : JSON.stringify(detail);
        apiError = new ApiError(res.status, message, { retryable: false });
      }
      if (isWrite && described) report({ status: "error", resource: described.resource, action: described.action, id: described.id, message: apiError.message, httpStatus: res.status });
      throw apiError;
    }
    if (isWrite && described) {
      const hint = data as WriteResponseHint | null;
      const writeBody = body as { name?: string; pipeline_name?: string } | undefined;
      report({
        status: "success", resource: described.resource, action: described.action,
        id: described.id || hint?.id || hint?.pipeline_id || hint?.connection_id || undefined,
        name: writeBody?.name || writeBody?.pipeline_name || hint?.name || undefined,
      });
    }
    return data as T;
  }

  return {
    baseUrl: base,
    health: (signal?: AbortSignal) => request<HealthResponse>("GET", "/health", { signal }),
    /** Who the caller is and what they may do — the UI's only source of permissions (contract 5.4). */
    me: (signal?: AbortSignal) => request<Me>("GET", "/me", { signal }),

    templates: {
      list: (signal?: AbortSignal) => request<ConnectorTemplate[]>("GET", "/connector-templates", { signal }),
      get: (id: string, signal?: AbortSignal) => request<ConnectorTemplate>("GET", `/connector-templates/${id}`, { signal }),
      create: (b: ConnectorTemplateCreate, signal?: AbortSignal) => request<ConnectorTemplate>("POST", "/connector-templates", { body: b, signal }),
      update: (id: string, b: ConnectorTemplateCreate, signal?: AbortSignal) => request<ConnectorTemplate>("PUT", `/connector-templates/${id}`, { body: b, signal }),
      remove: (id: string, signal?: AbortSignal) => request<void>("DELETE", `/connector-templates/${id}`, { signal }),
    },
    // Field metadata for a source type's connection_config — the single
    // definition also driving the backend's own validation/redaction (see
    // app/services/connection_config_schema.py). Global reference data
    // (not tenant-specific), same as templates above: adding a new source type is a
    // backend data change, never a new React form.
    connectionConfigSchemas: {
      list: (signal?: AbortSignal) => request<ConnectionConfigSchema[]>("GET", "/connection-config-schemas", { signal }),
    },
    connections: {
      list: (signal?: AbortSignal) => request<Connection[]>("GET", "/connections", { signal }),
      get: (id: string, signal?: AbortSignal) => request<Connection>("GET", `/connections/${id}`, { signal }),
      create: (b: ConnectionCreate, signal?: AbortSignal) => request<Connection>("POST", "/connections", { body: b, signal }),
      update: (id: string, b: ConnectionCreate, signal?: AbortSignal) => request<Connection>("PUT", `/connections/${id}`, { body: b, signal }),
      remove: (id: string, signal?: AbortSignal) => request<void>("DELETE", `/connections/${id}`, { signal }),
      test: (id: string, signal?: AbortSignal) => request<ConnectionTestResult>("POST", `/connections/${id}/test`, { signal }),
      testLive: (id: string, signal?: AbortSignal) => request<ConnectionTestResult>("POST", `/connections/${id}/test-connection`, { signal }),
      // connectionId is present only when probing an in-progress edit of an
      // already-saved connection — it's audit correlation only (see
      // app/api/routes/connections.py's test_connection_payload), the probe
      // still tests `b`, the current (possibly unsaved) form values, not
      // the saved row.
      testPayload: (b: ConnectionCreate, signal?: AbortSignal, connectionId?: string) =>
        request<ConnectionTestResult>("POST", "/connections/test-connection", {
          body: b, signal, query: connectionId ? { connection_id: connectionId } : undefined,
        }),
      // Discovery is live network I/O against the source system with no bound on
      // how long it takes, and — for a large source — can turn up far more
      // objects than belong in one response body. The backend submits it as an
      // Operation (202) driving a DiscoveryRun (see discovery.get/listObjects
      // below); poll discovery.get(id) for status/object_count, then page
      // through discovery.listObjects(id, ...) for the results themselves.
      discover: (id: string, signal?: AbortSignal) => request<DiscoveryRun>("POST", `/connections/${id}/discover`, { signal }),
    },
    pipelines: {
      list: (signal?: AbortSignal) => request<Pipeline[]>("GET", "/pipelines", { signal }),
      get: (id: string, signal?: AbortSignal) => request<Pipeline>("GET", `/pipelines/${id}`, { signal }),
      create: (b: PipelineCreate, signal?: AbortSignal) => request<Pipeline>("POST", "/pipelines", { body: b, signal }),
      // Backend exposes PATCH, not PUT, for partial pipeline updates.
      update: (id: string, b: PipelineUpdate, signal?: AbortSignal) => request<Pipeline>("PATCH", `/pipelines/${id}`, { body: b, signal }),
      remove: (id: string, signal?: AbortSignal) => request<void>("DELETE", `/pipelines/${id}`, { signal }),
      // deploy/pause/resume/restart all wait on a live Kafka Connect round-trip —
      // the backend submits them as an Operation (202) rather than holding the
      // request open; poll operations.get(id) for the eventual PipelineStatus
      // on its `result` field.
      // FE-BUG-24: the "Ready to deploy" review screen calls this BEFORE
      // deploy() below is ever submitted — read-only, never provisions or
      // deploys anything (see PipelineService.check_deploy_readiness).
      deployReadiness: (id: string, signal?: AbortSignal) => request<DeployReadiness>("GET", `/pipelines/${id}/deploy-readiness`, { signal }),
      deploy: (id: string, signal?: AbortSignal) => request<Operation>("POST", `/pipelines/${id}/deploy`, { signal }),
      pause: (id: string, signal?: AbortSignal) => request<Operation>("POST", `/pipelines/${id}/pause`, { signal }),
      resume: (id: string, signal?: AbortSignal) => request<Operation>("POST", `/pipelines/${id}/resume`, { signal }),
      restart: (id: string, signal?: AbortSignal) => request<Operation>("POST", `/pipelines/${id}/restart`, { signal }),
      status: (id: string, signal?: AbortSignal) => request<PipelineStatus>("GET", `/pipelines/${id}/status`, { signal }),
      generatedConfig: (id: string, signal?: AbortSignal) => request<GeneratedPipelineConfig>("GET", `/pipelines/${id}/generated-config`, { signal }),
      // FE-BUG-08: the real topic name, computed by the backend's own
      // app/services/topic_naming.py formula — never reconstructed
      // client-side, so the wizard's preview can't drift from what deploy
      // actually names the topic (see BUG-01).
      topicNamePreview: (
        identity: { zone: string; domain: string; source_system: string; entity: string; mode: string; version: string },
        signal?: AbortSignal,
      ) => request<TopicNamePreview>("GET", "/pipelines/topic-name-preview", { query: identity, signal }),
    },
    transforms: {
      list: (signal?: AbortSignal) => request<TransformPolicy[]>("GET", "/transform-policies", { signal }),
      get: (id: string, signal?: AbortSignal) => request<TransformPolicy>("GET", `/transform-policies/${id}`, { signal }),
      create: (b: TransformPolicyCreate, signal?: AbortSignal) => request<TransformPolicy>("POST", "/transform-policies", { body: b, signal }),
      update: (id: string, b: TransformPolicyCreate, signal?: AbortSignal) => request<TransformPolicy>("PUT", `/transform-policies/${id}`, { body: b, signal }),
      remove: (id: string, signal?: AbortSignal) => request<void>("DELETE", `/transform-policies/${id}`, { signal }),
      validate: (id: string, signal?: AbortSignal) => request<ValidationResult>("POST", `/transform-policies/${id}/validate`, { signal }),
      preview: (id: string, input_record: Record<string, unknown>, signal?: AbortSignal) => request<TransformPreviewResult>("POST", `/transform-policies/${id}/preview`, { body: { input_record }, signal }),
    },
    schemas: {
      list: (signal?: AbortSignal) => request<SchemaVersion[]>("GET", "/schemas", { signal }),
      get: (id: string, signal?: AbortSignal) => request<SchemaVersion>("GET", `/schemas/${id}`, { signal }),
      create: (b: SchemaVersionCreate, signal?: AbortSignal) => request<SchemaVersion>("POST", "/schemas", { body: b, signal }),
      infer: (records: Record<string, unknown>[], signal?: AbortSignal) => request<SchemaInferResult>("POST", "/schemas/infer", { body: { records }, signal }),
      addVersion: (id: string, b: SchemaVersionCreate, signal?: AbortSignal) => request<SchemaVersion>("POST", `/schemas/${id}/versions`, { body: b, signal }),
      validateCompat: (id: string, signal?: AbortSignal) => request<ValidationResult>("POST", `/schemas/${id}/validate-compatibility`, { signal }),
      driftEvents: (id: string, signal?: AbortSignal) => request<SchemaDriftEvent[]>("GET", `/schemas/${id}/drift-events`, { signal }),
      detectDrift: (id: string, records: Record<string, unknown>[], signal?: AbortSignal) => request<SchemaDriftResult>("POST", `/schemas/${id}/detect-drift`, { body: { records }, signal }),
      approvals: (status?: string, signal?: AbortSignal) => request<SchemaDriftApproval[]>("GET", "/schemas/drift-approvals", { query: { status }, signal }),
      approve: (id: string, b: SchemaDriftReviewRequest, signal?: AbortSignal) => request<SchemaDriftApproval>("POST", `/schemas/drift-approvals/${id}/approve`, { body: b, signal }),
      reject: (id: string, b: SchemaDriftReviewRequest, signal?: AbortSignal) => request<SchemaDriftApproval>("POST", `/schemas/drift-approvals/${id}/reject`, { body: b, signal }),
    },
    quality: {
      list: (signal?: AbortSignal) => request<QualityPolicy[]>("GET", "/quality-policies", { signal }),
      get: (id: string, signal?: AbortSignal) => request<QualityPolicy>("GET", `/quality-policies/${id}`, { signal }),
      create: (b: QualityPolicyCreate, signal?: AbortSignal) => request<QualityPolicy>("POST", "/quality-policies", { body: b, signal }),
      update: (id: string, b: QualityPolicyCreate, signal?: AbortSignal) => request<QualityPolicy>("PUT", `/quality-policies/${id}`, { body: b, signal }),
      remove: (id: string, signal?: AbortSignal) => request<void>("DELETE", `/quality-policies/${id}`, { signal }),
      validate: (id: string, signal?: AbortSignal) => request<ValidationResult>("POST", `/quality-policies/${id}/validate`, { signal }),
      capabilities: (signal?: AbortSignal) => request<QualityCapabilities>("GET", "/quality-policies/capabilities", { signal }),
      results: (signal?: AbortSignal) => request<QualityResult[]>("GET", "/quality-results", { signal }),
      // Live per-pipeline running totals from the continuous enforcement
      // worker — a different thing from results() above (a discrete
      // Execute-button/execute-job history); see QualityStreamStats's own
      // doc comment in types/api.ts.
      streamStats: (signal?: AbortSignal) => request<QualityStreamStats[]>("GET", "/quality-stream-stats", { signal }),
      // The UI's synchronous "Execute" action maps to the backend's /preview endpoint — there
      // is no /execute route (confirmed 404 against the live backend). /execute-jobs exists
      // separately for the backgrounded, job-based flow, which isn't wired up here.
      execute: (id: string, b: QualityExecutionRequest, signal?: AbortSignal) => request<QualityExecuteResult>("POST", `/quality-policies/${id}/preview`, { body: b, signal }),
      stream: (id: string, b: StreamingQualityRequest, signal?: AbortSignal) => request<StreamingQualityResult>("POST", `/quality-policies/${id}/stream/process`, { body: b, signal }),
    },
    lineage: {
      assets: (signal?: AbortSignal) => request<Asset[]>("GET", "/assets", { signal }),
      // FE-BUG-20: bounded expansion from one asset — depth/direction/
      // asset_type, never the whole graph. Used to grow the default
      // per-pipeline chain (graphPipeline below) outward one hop at a time
      // as the user expands upstream/downstream from a specific node.
      graphAsset: (
        id: string,
        opts: { depth?: number; direction?: "upstream" | "downstream" | "both"; assetType?: string; limit?: number; offset?: number } = {},
        signal?: AbortSignal,
      ) =>
        request<LineageGraph>("GET", `/lineage/${id}`, {
          signal,
          query: {
            depth: opts.depth, direction: opts.direction, asset_type: opts.assetType || undefined,
            // Width bound: the server caps each hop's edges and pages on with offset.
            limit: opts.limit, offset: opts.offset || undefined,
          },
        }),
      // The ticket's own default view — the fixed source→connector→raw
      // topic→quality→prepared topic chain materialized when a pipeline
      // deploys. Already bounded (one pipeline's own edges only).
      graphPipeline: (id: string, signal?: AbortSignal) => request<LineageGraph>("GET", `/lineage/pipeline/${id}`, { signal }),
      event: (b: LineageRuntimeEvent, signal?: AbortSignal) => request<LineageGraph>("POST", "/lineage/events", { body: b, signal }),
    },
    monitoring: {
      clusters: (signal?: AbortSignal) => request<ConnectCluster[]>("GET", "/runtime/connect-clusters", { signal }),
      clusterConnectors: (id: string, signal?: AbortSignal) => request<RuntimeConnector[]>("GET", `/runtime/connect-clusters/${id}/connectors`, { signal }),
      connectorStatus: (name: string, signal?: AbortSignal) => request<ConnectorStatus>("GET", `/runtime/connectors/${name}/status`, { signal }),
      connectorTasks: (name: string, signal?: AbortSignal) => request<ConnectorTask[]>("GET", `/runtime/connectors/${name}/tasks`, { signal }),
      // Prometheus-sourced throughput/lag/error series for one of this org's connectors (404 for anyone else's).
      connectorMetrics: (name: string, range: ConnectorMetricRange, signal?: AbortSignal) =>
        request<ConnectorMetrics>("GET", `/runtime/connectors/${encodeURIComponent(name)}/metrics`, { query: { range }, signal }),
      // Current rate / lag / task counts of EVERY connector this org owns, from one Prometheus query batch.
      connectorMetricsSummary: (signal?: AbortSignal) =>
        request<ConnectorMetricsSummary>("GET", "/runtime/connectors/metrics/summary", { signal }),
      metrics: (signal?: AbortSignal) => request<MetricsSummary>("GET", "/runtime/metrics/summary", { signal }),
      // FE-BUG-17: validation failures (by rule) and ingestion failures (by cause), as two separate groups.
      failures: (windowSeconds?: number, signal?: AbortSignal) => request<FailureBreakdown>("GET", "/runtime/metrics/failures", { query: windowSeconds ? { window_seconds: windowSeconds } : undefined, signal }),
      flow: (signal?: AbortSignal) => request<RuntimeFlow>("GET", "/runtime/flow", { signal }),
    },
    // Alerts: failures of this organization's pipelines raised by rules and posted to Slack / Teams (app/api/routes/alerts.py).
    alerts: {
      list: (q: { state: AlertState; from?: string; to?: string; limit: number; offset: number }, signal?: AbortSignal) =>
        request<AlertsPage>("GET", "/alerts", { query: { state: q.state, from: q.from, to: q.to, limit: q.limit, offset: q.offset }, signal }),
      counts: (signal?: AbortSignal) => request<AlertCounts>("GET", "/alerts/counts", { signal }),
      get: (id: string, signal?: AbortSignal) => request<AlertDetail>("GET", `/alerts/${id}`, { signal }),
      acknowledge: (id: string, note: string | null, signal?: AbortSignal) =>
        request<AlertDetail>("POST", `/alerts/${id}/acknowledge`, { body: { note: note || null }, signal }),
      resolve: (id: string, note: string | null, signal?: AbortSignal) =>
        request<AlertDetail>("POST", `/alerts/${id}/resolve`, { body: { note: note || null }, signal }),
      rules: (signal?: AbortSignal) => request<AlertRule[]>("GET", "/alerts/rules", { signal }),
      ruleKinds: (signal?: AbortSignal) => request<AlertRuleKindInfo[]>("GET", "/alerts/rules/kinds", { signal }),
      createRule: (b: AlertRuleCreate, signal?: AbortSignal) => request<AlertRule>("POST", "/alerts/rules", { body: b, signal }),
      updateRule: (id: string, b: AlertRulePatch, signal?: AbortSignal) => request<AlertRule>("PATCH", `/alerts/rules/${id}`, { body: b, signal }),
      deleteRule: (id: string, signal?: AbortSignal) => request<void>("DELETE", `/alerts/rules/${id}`, { signal }),
      runRules: (signal?: AbortSignal) => request<AlertSweepResult>("POST", "/alerts/rules/run", { signal }),
      channels: (signal?: AbortSignal) => request<AlertChannel[]>("GET", "/alerts/channels", { signal }),
      createChannel: (b: AlertChannelCreate, signal?: AbortSignal) => request<AlertChannel>("POST", "/alerts/channels", { body: b, signal }),
      updateChannel: (id: string, b: AlertChannelPatch, signal?: AbortSignal) =>
        request<AlertChannel>("PATCH", `/alerts/channels/${id}`, { body: b, signal }),
      deleteChannel: (id: string, signal?: AbortSignal) => request<void>("DELETE", `/alerts/channels/${id}`, { signal }),
      testChannel: (id: string, signal?: AbortSignal) => request<AlertChannelTestResult>("POST", `/alerts/channels/${id}/test`, { signal }),
    },
    audit: {
      list: (resourceType?: string, signal?: AbortSignal) =>
        request<AuditLogEntry[]>("GET", "/audit", { query: { resource_type: resourceType }, signal }),
    },
    operations: {
      get: (id: string, signal?: AbortSignal) => request<Operation>("GET", `/operations/${id}`, { signal }),
    },
    discovery: {
      get: (id: string, signal?: AbortSignal) => request<DiscoveryRun>("GET", `/discovery/${id}`, { signal }),
      // FE-BUG-12: object_type/schema_name are exact-match filters, backed by
      // a closed set of options from `facets` below — unlike `search`, which
      // is free text.
      listObjects: (
        id: string,
        opts: { search?: string; objectType?: string; schemaName?: string; limit?: number; cursor?: string } = {},
        signal?: AbortSignal,
      ) =>
        request<DiscoveryObjectPage>("GET", `/discovery/${id}/objects`, {
          signal,
          query: {
            search: opts.search || undefined,
            object_type: opts.objectType || undefined,
            schema_name: opts.schemaName || undefined,
            limit: opts.limit,
            cursor: opts.cursor,
          },
        }),
      // FE-BUG-12: distinct schema names/object types for a run, so the UI
      // can populate Schema/Type filter dropdowns without paging through
      // every discovered object first.
      facets: (id: string, signal?: AbortSignal) =>
        request<DiscoveryFacets>("GET", `/discovery/${id}/facets`, { signal }),
    },
  };
}
