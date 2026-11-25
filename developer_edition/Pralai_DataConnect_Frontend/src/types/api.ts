/*
 * Friendly domain type aliases over the generated OpenAPI schema
 * (dataconnect-api.generated.ts, regenerate with `npm run gen:api-types`).
 *
 * api.ts and the Redux store (src/store) import from here rather than the
 * generated file directly, so a schema rename only needs updating in one
 * place, and so the couple of response shapes the backend deliberately
 * leaves as passthrough JSON (runtime/flow, generated-config, ...) have one
 * hand-modeled home instead of being `any` at every call site.
 */
import type { components } from "./dataconnect-api.generated";

type Schemas = components["schemas"];

export type ConnectorTemplate = Schemas["ConnectorTemplate"];
export type ConnectorTemplateCreate = Schemas["ConnectorTemplateCreate"];
export type ConnectorTemplateRevision = Schemas["ConnectorTemplateRevision"];

export type Connection = Schemas["ConnectionProfile"];
export type ConnectionCreate = Schemas["ConnectionProfileCreate"];
export type ConnectionConfigSchema = Schemas["ConnectionConfigSchema"];
export type ConfigFieldSchema = Schemas["ConfigFieldSchema"];
export type ConnectionTestResult = Schemas["ConnectionTestResult"];

export type Pipeline = Schemas["Pipeline"];
export type PipelineCreate = Schemas["PipelineCreate"];
export type PipelineUpdate = Schemas["PipelineUpdate"];
export type PipelineStatus = Schemas["PipelineStatus"];
export type PipelineActionName = "deploy" | "pause" | "resume" | "restart";
export type DeployReadiness = Schemas["DeployReadiness"];
export type DeployReadinessCheck = Schemas["DeployReadinessCheck"];
// FE-BUG-08: the wizard asks the backend for this rather than reconstructing
// the topic name itself — see app/services/topic_naming.py (BUG-01) for why
// that used to drift from the real, canonical convention.
export type TopicNamePreview = Schemas["TopicNamePreview"];

export type TransformPolicy = Schemas["TransformPolicy"];
export type TransformPolicyCreate = Schemas["TransformPolicyCreate"];
export type TransformStep = Schemas["TransformStep"];
export type ValidationResult = Schemas["ValidationResult"];

export type SchemaVersion = Schemas["SchemaVersion"];
export type SchemaVersionCreate = Schemas["SchemaVersionCreate"];
export type SchemaField = Schemas["SchemaField"];
export type SchemaInferResult = Schemas["SchemaInferResult"];
export type SchemaDriftResult = Schemas["SchemaDriftResult"];
export type SchemaDriftApproval = Schemas["SchemaDriftApproval"];
export type SchemaDriftReviewRequest = Schemas["SchemaDriftReviewRequest"];
export type SchemaDriftDecision = "approve" | "reject";
/** GET .../schemas/{id}/drift-events — the backend declares this as passthrough JSON, no formal schema. */
export type SchemaDriftEvent = Record<string, unknown>;

export type QualityPolicy = Schemas["QualityPolicy"];
export type QualityPolicyCreate = Schemas["QualityPolicyCreate"];
/** GET /quality-policies/capabilities — which quarantine destinations and failure actions can actually be used (NEW-09). */
export type QualityCapabilities = Schemas["QualityCapabilities"];
export type QualityResult = Schemas["QualityResult"];
/** Live, continuously-updated per-pipeline totals from the real-time enforcement worker — see app/models/quality.py's QualityStreamStats docstring for why this is separate from QualityResult. */
export type QualityStreamStats = Schemas["QualityStreamStats"];
/**
 * One entry of QualityStreamStats.sample_failures / QualityExecuteResult.sample_failures — both are the backend's
 * build_quarantine_envelope() dict (app/services/quality.py), left as passthrough JSON on the generated schema.
 */
export interface QualityFailureEnvelope {
  /** The topic the failing record was read from — with several input topics, which one. */
  original_topic: string;
  /** Where in that topic (NEW-02). Absent for a failure from a manual check, which has no Kafka position. */
  original_partition?: number;
  original_offset?: number;
  pipeline_id: string;
  schema_id: string | null;
  quality_policy_id: string | null;
  failure_type: string;
  failed_rules: string[];
  received_at: string;
  original_record: Record<string, unknown>;
}
export type QualityExecutionRequest = Schemas["QualityExecutionRequest"];
/** The UI's "Execute" action calls the synchronous preview endpoint (see api.ts) — same response shape. */
export type QualityExecuteResult = Schemas["QualityPreviewResult"];
export type StreamingQualityRequest = Schemas["StreamingQualityRequest"];
export type StreamingQualityResult = Schemas["StreamingQualityResult"];

export type Asset = Schemas["Asset"];
export type LineageEdge = Schemas["LineageEdge"];
export type LineageGraph = Schemas["LineageGraph"];
export type LineageRuntimeEvent = Schemas["LineageRuntimeEvent"];

export type MetricsSummary = Schemas["MetricsSummary"];
export type FailureBreakdown = Schemas["FailureBreakdown"];
export type ConnectCluster = Schemas["ConnectCluster"];
export type RuntimeConnector = Schemas["RuntimeConnector"];
export type ConnectorStatus = Schemas["ConnectorStatus"];
export type ConnectorTask = Schemas["ConnectorTask"];
/** GET /runtime/connectors/{name}/metrics — Prometheus-sourced per-connector Kafka Connect metrics. */
export type ConnectorMetrics = Schemas["ConnectorMetrics"];
export type ConnectorMetricSeries = Schemas["ConnectorMetricSeries"];
export type ConnectorMetricPoint = Schemas["ConnectorMetricPoint"];
export type ConnectorMetricsSummary = Schemas["ConnectorMetricsSummary"];
export type ConnectorRate = Schemas["ConnectorRate"];
export type ConnectorTaskMetrics = Schemas["ConnectorTaskMetrics"];
export type ConnectorMetricRange = "15m" | "1h" | "6h" | "24h";

/** GET /health, /health/live — backend declares these as `Record<string, string>`. */
export interface HealthResponse {
  service?: string;
  [key: string]: string | undefined;
}

// FE-BUG-16: GET /runtime/flow now has a real response_model (RuntimeFlow —
// see app/schemas/monitoring.py) instead of untyped passthrough JSON, so
// `reachable`/`message` (previously invisible to this client) are properly
// typed. Its `connectors` entries are still a distinct, ad-hoc shape from the
// formal `RuntimeConnector` schema used by /runtime/connect-clusters/*\/connectors
// (name/type/state here vs. connector_name/observed_state there).
export type RuntimeFlow = Schemas["RuntimeFlow"];
export type RuntimeFlowConnector = Schemas["RuntimeFlowConnector"];
export type RuntimeFlowSink = Schemas["RuntimeFlowSink"];
export type RuntimeFlowTopic = Schemas["RuntimeFlowTopic"];

/** POST .../transform-policies/{id}/preview — backend declares this as passthrough JSON. */
export interface TransformPreviewResult {
  generated_config: Record<string, unknown>;
}

/** GET .../pipelines/{id}/generated-config — backend declares this as passthrough JSON. */
export type GeneratedPipelineConfig = Record<string, unknown>;

/** GET /audit */
export type AuditLogEntry = Schemas["AuditLogEntry"];

/**
 * GET /operations/{id} — a long-running action (pipeline deploy/pause/
 * resume/restart, connection discover) submitted as 202 + this resource
 * rather than held open on the request; poll until `status` is terminal.
 * `result` is whatever the old synchronous endpoint used to return
 * (PipelineStatus, DiscoveryResult, ...) — there's no single result type
 * across operation types, so it's untyped JSON on the wire; narrow it with
 * an `as` at the call site that knows which operation it submitted.
 */
export type Operation = Schemas["Operation"];

export type DiscoveryRun = Schemas["DiscoveryRun"];
export type DiscoveryObject = Schemas["DiscoveryObject"];
export type DiscoveryObjectPage = Schemas["DiscoveryObjectPage"];
export type DiscoveryFacets = Schemas["DiscoveryFacets"];
/** The SERVER's lifecycle for an Operation (GET /operations/{id}) — never mixed
 * with the browser's own observation state (`polling_timeout`/
 * `network_disconnected`, see store/pollUntilTerminal.ts). The wire type is a
 * plain string; this is what a caller narrows it to. `cancelled` is part of
 * the contract but the backend has no cancel action yet, so it is not emitted
 * today. */
export type OperationStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled" | "stale";

// ── Alerts (app/api/routes/alerts.py) ──────────────────────────────────────────────────────────────────────────────────────────
export type Alert = Schemas["AlertOut"];
export type AlertDetail = Schemas["AlertDetail"];
export type AlertEvent = Schemas["AlertEventOut"];
export type AlertsPage = Schemas["AlertsPage"];
export type AlertCounts = Schemas["AlertCounts"];
export type AlertState = Alert["state"];
export type AlertSeverity = Alert["severity"];
export type AlertRule = Schemas["RuleOut"];
export type AlertRuleCreate = Schemas["RuleCreate"];
export type AlertRulePatch = Schemas["RulePatch"];
export type AlertRuleKind = AlertRule["kind"];
export type AlertRuleKindInfo = Schemas["RuleKindInfo"];
export type AlertChannel = Schemas["ChannelOut"];
export type AlertChannelCreate = Schemas["ChannelCreate"];
export type AlertChannelPatch = Schemas["ChannelPatch"];
export type AlertChannelKind = AlertChannel["kind"];
export type AlertChannelTestResult = Schemas["ChannelTestResult"];
export type AlertSweepResult = Schemas["SweepOut"];
