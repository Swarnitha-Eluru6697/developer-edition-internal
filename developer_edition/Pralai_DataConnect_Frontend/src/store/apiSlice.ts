/*
 * RTK Query slice — the single source of truth for all DataConnect server
 * state (connections, pipelines, schemas, transforms, quality, lineage,
 * runtime/monitoring, templates, health). Replaces the old per-page
 * `useLoad` + manual fetch pattern: every page now shares one cache keyed by
 * endpoint + args, so e.g. Dashboard and Pipelines both reading the pipeline
 * list hit the network once and stay in sync via tag invalidation.
 *
 * There's no `fetchBaseQuery` here — `queryFn` calls straight into the
 * existing `api.ts` client (created per store, see store.ts) so all of its
 * behavior (X-Request-Id header, ApiError shape, write-operation
 * telemetry to the host) is reused rather than reimplemented. Every
 * endpoint's arg/result type comes from src/types/api.ts (generated from the
 * backend's OpenAPI schema), not `any`.
 *
 * Cancellation: every queryFn receives RTK Query's own per-request
 * `AbortSignal` (aborted automatically on unmount-with-no-other-subscribers,
 * or when a superseding request is issued for the same query) and forwards
 * it into the matching api.ts client call, which passes it straight to
 * `fetch()`. Without this wiring the signal RTK Query hands us went nowhere
 * — the query would stop being *watched* on unmount or arg change, but the
 * underlying HTTP request kept running to completion regardless (a stale
 * search, a page you navigated away from, a poll tick overlapping the next
 * one). See api.ts's own header comment for the fetch()-level half of this.
 */
import { createApi, fakeBaseQuery } from "@reduxjs/toolkit/query/react";
import { apiErrorShape, errorMessage, type ApiErrorShape } from "../errors";
import type { StoreExtra } from "./store";
import type { Me } from "../permissions";
import type {
  AlertChannel, AlertChannelCreate, AlertChannelPatch, AlertChannelTestResult, AlertCounts, AlertDetail, AlertRule, AlertRuleCreate,
  AlertRuleKindInfo, AlertRulePatch, AlertsPage, AlertState, AlertSweepResult,
  Asset, AuditLogEntry, ConnectCluster, Connection, ConnectionConfigSchema, ConnectionCreate, ConnectionTestResult, ConnectorMetricRange, ConnectorMetrics, ConnectorMetricsSummary, ConnectorStatus,
  ConnectorTemplate, DeployReadiness, DiscoveryFacets, DiscoveryObjectPage, DiscoveryRun, FailureBreakdown, HealthResponse, LineageGraph, MetricsSummary, Operation, Pipeline,
  PipelineActionName, PipelineCreate, PipelineUpdate, QualityExecuteResult, QualityExecutionRequest,
  QualityCapabilities, QualityPolicy, QualityPolicyCreate, QualityResult, QualityStreamStats, RuntimeFlow, SchemaDriftApproval,
  SchemaDriftDecision, SchemaDriftEvent, SchemaDriftResult, SchemaDriftReviewRequest,
  SchemaInferResult, SchemaVersion, SchemaVersionCreate, TopicNamePreview, TransformPolicy, TransformPolicyCreate,
  TransformPreviewResult, ValidationResult,
} from "../types/api";

/**
 * What every RTK Query hook's `error` looks like across this whole app —
 * mirrors ApiError (src/api.ts), which itself mirrors the backend's
 * ErrorEnvelope (app/schemas/errors.py). `code`/`fieldErrors`/`requestId`
 * are undefined for a pre-envelope or non-DataConnect response (see
 * ApiError's own doc comment); `retryable` always has a value (defaults to
 * false) so a page can safely render a "Retry" affordance only when it's
 * actually true, without an extra null check.
 */
type QueryError = ApiErrorShape;

function toQueryError(e: unknown): QueryError {
  if (e instanceof Error && e.name === "AbortError") {
    // Cancelled on purpose (unmount, superseded request) — by the time this
    // ever reaches a component, RTK Query has already stopped it being the
    // "current" result for anything subscribed, so this shape only exists
    // to make an accidental console.log of it read as "cancelled", never as
    // a network failure.
    return { status: 0, message: "Request cancelled", retryable: false };
  }
  return apiErrorShape(e) ?? { status: 0, message: errorMessage(e), retryable: false };
}

async function call<T>(
  extra: unknown,
  signal: AbortSignal,
  fn: (client: StoreExtra["client"], signal: AbortSignal) => Promise<T>,
) {
  try {
    return { data: await fn((extra as StoreExtra).client, signal) };
  } catch (e) {
    return { error: toQueryError(e) };
  }
}

export const dataConnectApi = createApi({
  reducerPath: "dataConnectApi",
  baseQuery: fakeBaseQuery<QueryError>(),
  // RTK Query's default (60s) keeps an unsubscribed query's cache entry —
  // and its in-flight request — alive well past the point every component
  // watching it has unmounted, so a page transition wouldn't actually abort
  // anything for a full minute. 0 means the moment the last subscriber goes
  // away, the entry (and the signal passed into the matching api.ts call)
  // is torn down immediately, which is what actually cancels the request
  // instead of merely stopping the UI from caring about it. The tradeoff —
  // navigating back to a page refetches instead of showing cached data —
  // is the right side of that tradeoff for the write-heavy, mostly-small
  // responses this app deals with.
  keepUnusedDataFor: 0,
  tagTypes: [
    "Health", "Templates", "Connections", "Pipelines", "Transforms",
    "Schemas", "SchemaDrift", "SchemaApprovals", "Quality", "QualityResults", "QualityStreamStats",
    "Lineage", "Monitoring", "Audit", "Operations", "Discovery", "Alerts", "AlertRules", "AlertChannels",
  ],
  endpoints: (builder) => ({
    // ── Health ──────────────────────────────────────────────────────────────
    getMe: builder.query<Me, void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.me(s)),
    }),
    getHealth: builder.query<HealthResponse, void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.health(s)),
      providesTags: ["Health"],
    }),

    // ── Templates ───────────────────────────────────────────────────────────
    listTemplates: builder.query<ConnectorTemplate[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.templates.list(s)),
      providesTags: ["Templates"],
    }),
    getTemplate: builder.query<ConnectorTemplate, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.templates.get(id, s)),
      providesTags: (_r, _e, id) => [{ type: "Templates", id }],
    }),

    // Field metadata per source_type (app/services/connection_config_schema.py)
    // — static reference data, never mutated by anything this app does, so
    // no tag: nothing ever needs to invalidate it.
    listConnectionConfigSchemas: builder.query<ConnectionConfigSchema[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.connectionConfigSchemas.list(s)),
    }),

    // ── Connections ─────────────────────────────────────────────────────────
    getConnections: builder.query<Connection[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.connections.list(s)),
      providesTags: (r) => [
        "Connections",
        ...((r || []).map((c) => ({ type: "Connections" as const, id: c.id }))),
      ],
    }),
    getConnection: builder.query<Connection, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.connections.get(id, s)),
      providesTags: (_r, _e, id) => [{ type: "Connections", id }],
    }),
    createConnection: builder.mutation<Connection, ConnectionCreate>({
      queryFn: (body, { extra, signal }) => call(extra, signal, (c, s) => c.connections.create(body, s)),
      invalidatesTags: ["Connections"],
    }),
    updateConnection: builder.mutation<Connection, { id: string; body: ConnectionCreate }>({
      queryFn: ({ id, body }, { extra, signal }) => call(extra, signal, (c, s) => c.connections.update(id, body, s)),
      invalidatesTags: (_r, _e, { id }) => ["Connections", { type: "Connections", id }],
    }),
    deleteConnection: builder.mutation<void, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.connections.remove(id, s)),
      invalidatesTags: ["Connections"],
    }),
    testConnectionLive: builder.mutation<ConnectionTestResult, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.connections.testLive(id, s)),
    }),
    testConnectionPayload: builder.mutation<ConnectionTestResult, { body: ConnectionCreate; connectionId?: string }>({
      queryFn: ({ body, connectionId }, { extra, signal }) =>
        call(extra, signal, (c, s) => c.connections.testPayload(body, s, connectionId)),
    }),
    // Returns 202 + a queued DiscoveryRun (GAP-10), not the discovered
    // objects themselves — see src/store/pollDiscoveryRun.ts for the
    // "submit, then poll" helper, and getDiscoveryObjects below for paging
    // through the results once it's done.
    discoverConnection: builder.mutation<DiscoveryRun, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.connections.discover(id, s)),
    }),

    // ── Pipelines ───────────────────────────────────────────────────────────
    getPipelines: builder.query<Pipeline[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.pipelines.list(s)),
      providesTags: (r) => [
        "Pipelines",
        ...((r || []).map((p) => ({ type: "Pipelines" as const, id: p.id }))),
      ],
    }),
    getPipeline: builder.query<Pipeline, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.pipelines.get(id, s)),
      providesTags: (_r, _e, id) => [{ type: "Pipelines", id }],
    }),
    // FE-BUG-24: lazy, not auto-fetched — the "Ready to deploy" screen
    // triggers this itself when opened, and it's meaningless to cache
    // against "Pipelines" tag invalidation the way getPipeline is (its
    // result depends on live Kafka Connect / source-connectivity state
    // that changes independently of this app's own writes).
    getDeployReadiness: builder.query<DeployReadiness, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.pipelines.deployReadiness(id, s)),
    }),
    // FE-BUG-08: lazy, triggered (debounced) as the "New pipeline" wizard's
    // identity fields change — never cached against "Pipelines", since it
    // has nothing to do with any pipeline that actually exists yet.
    getTopicNamePreview: builder.query<
      TopicNamePreview,
      { zone: string; domain: string; source_system: string; entity: string; mode: string; version: string }
    >({
      queryFn: (identity, { extra, signal }) => call(extra, signal, (c, s) => c.pipelines.topicNamePreview(identity, s)),
    }),
    createPipeline: builder.mutation<Pipeline, PipelineCreate>({
      queryFn: (body, { extra, signal }) => call(extra, signal, (c, s) => c.pipelines.create(body, s)),
      invalidatesTags: ["Pipelines", "Monitoring", "Lineage"],
    }),
    // FE-BUG-14: `body.expected_revision` (see PipelineUpdate) is the
    // optimistic-concurrency check — a 409 PIPELINE_VERSION_CONFLICT means
    // someone else saved first; the error's own `details.current` carries
    // the pipeline's latest state, so the caller doesn't need a second
    // request to show what changed. See PipelineEditForm in Pipelines.tsx.
    updatePipeline: builder.mutation<Pipeline, { id: string; body: PipelineUpdate }>({
      queryFn: ({ id, body }, { extra, signal }) => call(extra, signal, (c, s) => c.pipelines.update(id, body, s)),
      invalidatesTags: (_r, _e, { id }) => ["Pipelines", { type: "Pipelines", id }],
    }),
    deletePipeline: builder.mutation<void, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.pipelines.remove(id, s)),
      invalidatesTags: ["Pipelines", "Monitoring", "Lineage"],
    }),
    // Returns 202 + a queued Operation, not the PipelineStatus itself — see
    // src/store/pollOperation.ts for the "submit, then poll" helper callers use.
    // Tags still invalidate at submission time (not once the operation
    // actually completes) — a harmless imprecision given the background
    // task typically finishes within the same network round-trip the
    // invalidated refetch takes, and the Pipelines page's own polling
    // catches up regardless; pollOperation's caller invalidates again once
    // the real outcome is known, for immediate feedback.
    pipelineAction: builder.mutation<Operation, { id: string; action: PipelineActionName }>({
      queryFn: ({ id, action }, { extra, signal }) => call(extra, signal, (c, s) => c.pipelines[action](id, s)),
      invalidatesTags: (_r, _e, { id }) => ["Pipelines", { type: "Pipelines", id }, "Monitoring", "Lineage"],
    }),

    // ── Transforms ──────────────────────────────────────────────────────────
    getTransforms: builder.query<TransformPolicy[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.transforms.list(s)),
      providesTags: ["Transforms"],
    }),
    createTransform: builder.mutation<TransformPolicy, TransformPolicyCreate>({
      queryFn: (body, { extra, signal }) => call(extra, signal, (c, s) => c.transforms.create(body, s)),
      invalidatesTags: ["Transforms"],
    }),
    deleteTransform: builder.mutation<void, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.transforms.remove(id, s)),
      invalidatesTags: ["Transforms"],
    }),
    validateTransform: builder.mutation<ValidationResult, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.transforms.validate(id, s)),
    }),
    previewTransform: builder.mutation<TransformPreviewResult, { id: string; record: Record<string, unknown> }>({
      queryFn: ({ id, record }, { extra, signal }) => call(extra, signal, (c, s) => c.transforms.preview(id, record, s)),
    }),

    // ── Schemas ─────────────────────────────────────────────────────────────
    getSchemas: builder.query<SchemaVersion[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.schemas.list(s)),
      providesTags: ["Schemas"],
    }),
    getSchema: builder.query<SchemaVersion, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.schemas.get(id, s)),
      providesTags: (_r, _e, id) => [{ type: "Schemas", id }],
    }),
    createSchema: builder.mutation<SchemaVersion, SchemaVersionCreate>({
      queryFn: (body, { extra, signal }) => call(extra, signal, (c, s) => c.schemas.create(body, s)),
      invalidatesTags: ["Schemas"],
    }),
    inferSchema: builder.mutation<SchemaInferResult, Record<string, unknown>[]>({
      queryFn: (records, { extra, signal }) => call(extra, signal, (c, s) => c.schemas.infer(records, s)),
    }),
    getSchemaDriftEvents: builder.query<SchemaDriftEvent[], string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.schemas.driftEvents(id, s)),
      providesTags: (_r, _e, id) => [{ type: "SchemaDrift", id }],
    }),
    detectSchemaDrift: builder.mutation<SchemaDriftResult, { id: string; records: Record<string, unknown>[] }>({
      queryFn: ({ id, records }, { extra, signal }) => call(extra, signal, (c, s) => c.schemas.detectDrift(id, records, s)),
      invalidatesTags: (_r, _e, { id }) => [{ type: "SchemaDrift", id }, "SchemaApprovals"],
    }),
    getDriftApprovals: builder.query<SchemaDriftApproval[], string | void>({
      queryFn: (status, { extra, signal }) => call(extra, signal, (c, s) => c.schemas.approvals(status || undefined, s)),
      providesTags: ["SchemaApprovals"],
    }),
    decideDriftApproval: builder.mutation<SchemaDriftApproval, { id: string; action: SchemaDriftDecision; body: SchemaDriftReviewRequest }>({
      queryFn: ({ id, action, body }, { extra, signal }) => call(extra, signal, (c, s) => c.schemas[action](id, body, s)),
      invalidatesTags: ["SchemaApprovals", "Schemas"],
    }),

    // ── Quality ─────────────────────────────────────────────────────────────
    getQualityPolicies: builder.query<QualityPolicy[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.quality.list(s)),
      providesTags: ["Quality"],
    }),
    getQualityCapabilities: builder.query<QualityCapabilities, void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.quality.capabilities(s)),
    }),
    getQualityPolicy: builder.query<QualityPolicy, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.quality.get(id, s)),
      providesTags: (_r, _e, id) => [{ type: "Quality", id }],
    }),
    createQualityPolicy: builder.mutation<QualityPolicy, QualityPolicyCreate>({
      queryFn: (body, { extra, signal }) => call(extra, signal, (c, s) => c.quality.create(body, s)),
      invalidatesTags: ["Quality"],
    }),
    deleteQualityPolicy: builder.mutation<void, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.quality.remove(id, s)),
      invalidatesTags: ["Quality"],
    }),
    validateQualityPolicy: builder.mutation<ValidationResult, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.quality.validate(id, s)),
    }),
    getQualityResults: builder.query<QualityResult[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.quality.results(s)),
      providesTags: ["QualityResults"],
    }),
    // Live per-pipeline totals from the continuous enforcement worker — a
    // different tag from QualityResults above (that's the discrete
    // Execute-button/execute-job history) since the two never invalidate
    // together.
    getQualityStreamStats: builder.query<QualityStreamStats[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.quality.streamStats(s)),
      providesTags: ["QualityStreamStats"],
    }),
    executeQualityPolicy: builder.mutation<QualityExecuteResult, { id: string; body: QualityExecutionRequest }>({
      queryFn: ({ id, body }, { extra, signal }) => call(extra, signal, (c, s) => c.quality.execute(id, body, s)),
      invalidatesTags: ["QualityResults"],
    }),

    // ── Lineage ─────────────────────────────────────────────────────────────
    getLineageAssets: builder.query<Asset[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.lineage.assets(s)),
      providesTags: ["Lineage"],
    }),
    getPipelineLineageGraph: builder.query<LineageGraph, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.lineage.graphPipeline(id, s)),
      providesTags: (_r, _e, id) => [{ type: "Lineage", id }],
    }),
    // FE-BUG-20: on-demand (lazy) — fired only when the user clicks
    // "expand upstream"/"expand downstream" on a specific node, not
    // fetched eagerly for every asset in the graph.
    getAssetLineage: builder.query<
      LineageGraph,
      { id: string; depth?: number; direction?: "upstream" | "downstream" | "both"; assetType?: string; limit?: number; offset?: number }
    >({
      queryFn: ({ id, ...opts }, { extra, signal }) => call(extra, signal, (c, s) => c.lineage.graphAsset(id, opts, s)),
      providesTags: (_r, _e, { id }) => [{ type: "Lineage", id: `${id}-expand` }],
    }),

    // ── Monitoring / runtime ────────────────────────────────────────────────
    getRuntimeMetrics: builder.query<MetricsSummary, void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.monitoring.metrics(s)),
      providesTags: ["Monitoring"],
    }),
    // The argument is how far back to count, in seconds (the Quality page's "Last 15m / 1h / 6h / 24h"); none = the backend's own window.
    getFailureBreakdown: builder.query<FailureBreakdown, number | void>({
      queryFn: (windowSeconds, { extra, signal }) => call(extra, signal, (c, s) => c.monitoring.failures(windowSeconds || undefined, s)),
      providesTags: ["Monitoring", "QualityStreamStats"],
    }),
    getRuntimeFlow: builder.query<RuntimeFlow, void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.monitoring.flow(s)),
      providesTags: ["Monitoring"],
    }),
    getConnectClusters: builder.query<ConnectCluster[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.monitoring.clusters(s)),
      providesTags: ["Monitoring"],
    }),
    getConnectorStatus: builder.query<ConnectorStatus, string>({
      queryFn: (name, { extra, signal }) => call(extra, signal, (c, s) => c.monitoring.connectorStatus(name, s)),
      providesTags: (_r, _e, name) => [{ type: "Monitoring", id: name }],
    }),
    getConnectorMetricsSummary: builder.query<ConnectorMetricsSummary, void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.monitoring.connectorMetricsSummary(s)),
      providesTags: [{ type: "Monitoring", id: "connector-metrics-summary" }],
    }),
    getConnectorMetrics: builder.query<ConnectorMetrics, { name: string; range: ConnectorMetricRange }>({
      queryFn: ({ name, range }, { extra, signal }) => call(extra, signal, (c, s) => c.monitoring.connectorMetrics(name, range, s)),
      providesTags: (_r, _e, { name }) => [{ type: "Monitoring", id: `${name}-metrics` }],
    }),

    // ── Audit / history ─────────────────────────────────────────────────────
    // ── Alerts ──────────────────────────────────────────────────────────────
    listAlerts: builder.query<AlertsPage, { state: AlertState; from?: string; to?: string; limit: number; offset: number }>({
      queryFn: (q, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.list(q, s)),
      providesTags: ["Alerts"],
    }),
    getAlertCounts: builder.query<AlertCounts, void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.counts(s)),
      providesTags: ["Alerts"],
    }),
    getAlert: builder.query<AlertDetail, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.get(id, s)),
      providesTags: ["Alerts"],
    }),
    acknowledgeAlert: builder.mutation<AlertDetail, { id: string; note: string | null }>({
      queryFn: ({ id, note }, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.acknowledge(id, note, s)),
      invalidatesTags: ["Alerts", "Audit"],
    }),
    resolveAlert: builder.mutation<AlertDetail, { id: string; note: string | null }>({
      queryFn: ({ id, note }, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.resolve(id, note, s)),
      invalidatesTags: ["Alerts", "Audit"],
    }),
    listAlertRules: builder.query<AlertRule[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.rules(s)),
      providesTags: ["AlertRules"],
    }),
    listAlertRuleKinds: builder.query<AlertRuleKindInfo[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.ruleKinds(s)),
    }),
    createAlertRule: builder.mutation<AlertRule, AlertRuleCreate>({
      queryFn: (b, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.createRule(b, s)),
      invalidatesTags: ["AlertRules", "Audit"],
    }),
    updateAlertRule: builder.mutation<AlertRule, { id: string; body: AlertRulePatch }>({
      queryFn: ({ id, body }, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.updateRule(id, body, s)),
      invalidatesTags: ["AlertRules", "Audit"],
    }),
    deleteAlertRule: builder.mutation<void, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.deleteRule(id, s)),
      invalidatesTags: ["AlertRules", "Audit"],
    }),
    runAlertRules: builder.mutation<AlertSweepResult, void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.runRules(s)),
      invalidatesTags: ["Alerts", "AlertChannels", "Audit"],
    }),
    listAlertChannels: builder.query<AlertChannel[], void>({
      queryFn: (_arg, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.channels(s)),
      providesTags: ["AlertChannels"],
    }),
    createAlertChannel: builder.mutation<AlertChannel, AlertChannelCreate>({
      queryFn: (b, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.createChannel(b, s)),
      invalidatesTags: ["AlertChannels", "Audit"],
    }),
    updateAlertChannel: builder.mutation<AlertChannel, { id: string; body: AlertChannelPatch }>({
      queryFn: ({ id, body }, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.updateChannel(id, body, s)),
      invalidatesTags: ["AlertChannels", "Audit"],
    }),
    deleteAlertChannel: builder.mutation<void, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.deleteChannel(id, s)),
      invalidatesTags: ["AlertChannels", "Audit"],
    }),
    testAlertChannel: builder.mutation<AlertChannelTestResult, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.alerts.testChannel(id, s)),
      invalidatesTags: ["AlertChannels", "Audit"],
    }),

    getAuditLogs: builder.query<AuditLogEntry[], string | void>({
      queryFn: (resourceType, { extra, signal }) => call(extra, signal, (c, s) => c.audit.list(resourceType || undefined, s)),
      providesTags: ["Audit"],
    }),

    // ── Operations (long-running deploy/pause/resume/restart/discover) ──────
    getOperation: builder.query<Operation, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.operations.get(id, s)),
      providesTags: (_r, _e, id) => [{ type: "Operations", id }],
    }),

    // ── Discovery (GAP-10) ───────────────────────────────────────────────────
    getDiscoveryRun: builder.query<DiscoveryRun, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.discovery.get(id, s)),
      providesTags: (_r, _e, id) => [{ type: "Discovery", id }],
    }),
    getDiscoveryObjects: builder.query<
      DiscoveryObjectPage,
      { id: string; search?: string; objectType?: string; schemaName?: string; limit?: number; cursor?: string }
    >({
      queryFn: ({ id, ...opts }, { extra, signal }) => call(extra, signal, (c, s) => c.discovery.listObjects(id, opts, s)),
      providesTags: (_r, _e, { id }) => [{ type: "Discovery", id: `${id}-objects` }],
    }),
    // FE-BUG-12: static per-run facets (schema names/object types) — not
    // invalidated by anything, a discovery run's objects never change after
    // it completes (see DiscoveryObject's own model docstring).
    getDiscoveryFacets: builder.query<DiscoveryFacets, string>({
      queryFn: (id, { extra, signal }) => call(extra, signal, (c, s) => c.discovery.facets(id, s)),
      providesTags: (_r, _e, id) => [{ type: "Discovery", id: `${id}-facets` }],
    }),
  }),
});

export const {
  useListAlertsQuery, useGetAlertCountsQuery, useGetAlertQuery, useAcknowledgeAlertMutation, useResolveAlertMutation,
  useListAlertRulesQuery, useListAlertRuleKindsQuery, useCreateAlertRuleMutation, useUpdateAlertRuleMutation, useDeleteAlertRuleMutation,
  useRunAlertRulesMutation, useListAlertChannelsQuery, useCreateAlertChannelMutation, useUpdateAlertChannelMutation,
  useDeleteAlertChannelMutation, useTestAlertChannelMutation,
  useGetMeQuery,
  useGetHealthQuery,
  useListTemplatesQuery,
  useGetTemplateQuery,
  useListConnectionConfigSchemasQuery,
  useGetConnectionsQuery,
  useLazyGetConnectionQuery,
  useCreateConnectionMutation,
  useUpdateConnectionMutation,
  useDeleteConnectionMutation,
  useTestConnectionLiveMutation,
  useTestConnectionPayloadMutation,
  useDiscoverConnectionMutation,
  useGetPipelinesQuery,
  useGetPipelineQuery,
  useLazyGetPipelineQuery,
  useLazyGetDeployReadinessQuery,
  useLazyGetTopicNamePreviewQuery,
  useCreatePipelineMutation,
  useUpdatePipelineMutation,
  useDeletePipelineMutation,
  usePipelineActionMutation,
  useGetTransformsQuery,
  useCreateTransformMutation,
  useDeleteTransformMutation,
  useValidateTransformMutation,
  usePreviewTransformMutation,
  useGetSchemasQuery,
  useGetSchemaQuery,
  useCreateSchemaMutation,
  useInferSchemaMutation,
  useGetSchemaDriftEventsQuery,
  useDetectSchemaDriftMutation,
  useGetDriftApprovalsQuery,
  useDecideDriftApprovalMutation,
  useGetQualityPoliciesQuery,
  useGetQualityCapabilitiesQuery,
  useGetQualityPolicyQuery,
  useCreateQualityPolicyMutation,
  useDeleteQualityPolicyMutation,
  useValidateQualityPolicyMutation,
  useGetQualityResultsQuery,
  useGetQualityStreamStatsQuery,
  useExecuteQualityPolicyMutation,
  useGetLineageAssetsQuery,
  useGetPipelineLineageGraphQuery,
  useLazyGetAssetLineageQuery,
  useGetRuntimeMetricsQuery,
  useGetFailureBreakdownQuery,
  useGetRuntimeFlowQuery,
  useGetConnectClustersQuery,
  useGetConnectorStatusQuery,
  useGetConnectorMetricsQuery,
  useGetConnectorMetricsSummaryQuery,
  useGetAuditLogsQuery,
  useGetOperationQuery,
  useLazyGetOperationQuery,
  useGetDiscoveryRunQuery,
  useLazyGetDiscoveryRunQuery,
  useGetDiscoveryObjectsQuery,
  useLazyGetDiscoveryObjectsQuery,
  useGetDiscoveryFacetsQuery,
} = dataConnectApi;
