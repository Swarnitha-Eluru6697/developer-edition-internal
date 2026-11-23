/**
 * Frontend contract types (WS-FE-014).
 *
 * Every type that mirrors a REST request/response is an alias into the
 * generated OpenAPI schema (src/api/generated/workspace-schema.ts, produced by
 * `npm run codegen:api` from the committed snapshot openapi/workspace.v0.1.0.json
 * — see openapi/README.md). That includes the dbt / FlinkSQL / Data Quality
 * proxy routes: the backend now declares response models for them
 * (app/schemas/proxy.py), so they are no longer hand-copied from each
 * service's contract. A backend contract change therefore shows up here as a
 * compile error the next time someone regenerates, not as silent drift.
 *
 * What is still hand-written, and why:
 *   - WebSocket envelopes/payloads → src/ws/types.ts. OpenAPI covers REST only.
 *   - `GitActionResult`, `WorkspaceFromGitResult`, `BranchInfo` and
 *     `ExecutionResult` below: their backend handlers (app/api/git.py,
 *     app/api/executions.py) return a bare dict with no response_model, so the
 *     spec types them as an open object. Replace them with aliases once those
 *     routes declare models.
 *   - Narrowed vocabularies the spec leaves as `string` (CodegenRole,
 *     CodegenValidationStatus, GitProvider), the per-block shape of
 *     CodegenMessage.content (an untyped list in the spec), and the flattened
 *     CurrentUser view the getMe transform produces.
 *   - The /api/v1/me and /api/v1/auth/me bodies (see Auth below).
 *   - Frontend-only UI state (ExecutionState).
 */
import type { JsonRequestBody, Schemas } from "../api/contract";

export type UUID = string;
export type ISODate = string;

// ---------- Auth ----------
// Hand-written on purpose: these two bodies changed shape with role-free authorisation (permissions instead of roles), and the
// SPA must compile against the generated schema both before and after it is regenerated from the backend.

/** GET /api/v1/me (platform permission contract, section 5.4): who the caller is and every permission they hold. */
export interface MyPermissionsResponse {
  user: string;
  userId: string;
  tenant: string;
  permissions: string[];
}

/** GET /api/v1/auth/me as the backend sends it (nested user/claims). Its `permissions` mirror /api/v1/me; the UI gates on
 *  /api/v1/me (src/rbac/hooks.ts), not on this. */
export interface MeResponse {
  user: { id: string; external_id: string; email: string | null; display_name: string | null };
  claims: { user_id: string; org_id: string; email: string | null; username?: string | null };
  permissions?: string[];
  workspace_model: string;
}

/** The flattened identity view the SPA works with — getMe's transformResponse (features/workspace/me.ts) builds it from MeResponse. */
export interface CurrentUser {
  user_id: string;
  org_id: string;
  email: string;
  display_name: string;
}

// ---------- Workspaces ----------
export type Workspace = Schemas["WorkspaceOut"];
export type WorkspaceType = Schemas["WorkspaceOut"]["workspace_type"];
export type WorkspaceCreate = Schemas["WorkspaceCreate"];
export type WorkspaceUpdate = Schemas["WorkspaceUpdate"];

// ---------- Files ----------
// "flinksql" is authored in the same Monaco editor as "sql" (files are named
// *.flink.sql) but targets the FlinkSQL Runner, never the lakehouse.
export type FileLanguage = Schemas["FileCreate"]["language"];
// FileMetadata is the real name of what components call WorkspaceFile.
export type WorkspaceFile = Schemas["FileMetadata"];
export type FileTreeNode = Schemas["FileTreeNode"];
export type FileWithContent = Schemas["FileWithContent"];
export type FileCreate = Schemas["FileCreate"];
// `if_match_sha256` in the JSON body is the backend's optimistic-concurrency
// field (app/services/file_service.py) — there is no If-Match header check.
export type FileUpdate = Schemas["FileUpdate"];

// ---------- Git ----------
export type GitProvider = "github" | "gitlab" | "azure_devops";
export type GitBinding = Schemas["GitBindingOut"];
export type CommitRequest = Schemas["CommitRequest"];
export type CommitOut = Schemas["CommitOut"];
export type GitStatus = Schemas["GitStatus"];
export type RepoListItem = Schemas["RepoListItem"];
export type PullRequestCreate = Schemas["PullRequestCreate"];
export type PullRequestOut = Schemas["PullRequestOut"];
export type WorkspaceFromGitCreate = Schemas["WorkspaceFromGitCreate"];
export type OAuthInitiate = Schemas["OAuthInitiateOut"];
// Hand-written: app/api/git.py returns bare dicts for these (no response_model).
export interface BranchInfo { branch: string }
export interface GitActionResult { status: string; branch: string; files_synced?: number }
export interface WorkspaceFromGitResult {
  workspace_id: UUID;
  workspace_name: string;
  branch: string;
  files_imported?: number;
  import_error?: string | null;
}

// ---------- Executions ----------
export type ExecutionSession = Schemas["SessionOut"];
export type SqlExecutionRequest = Schemas["SqlExecutionRequest"];
export type NotebookCellExecutionRequest = Schemas["NotebookCellExecutionRequest"];
export type ExecutionAccepted = Schemas["ExecutionAccepted"];
export type ExecutionHistoryItem = Schemas["ExecutionOut"];
/** GET /api/v1/executions/{id}/result — hand-written: app/api/executions.py
 *  returns a bare dict for it. `outputs` is set for notebook (Python) runs. */
/** One statement's result of a SQL script, as GET /executions/{id}/result sends it. */
export interface ExecutionResultSet {
  statement: number;
  sql?: string | null;
  columns: { name: string; type: string }[];
  rows: unknown[][];
  row_count?: number | null;
  truncated?: boolean;
  status?: string | null;   // command tag of a statement that returns no rows, e.g. "INSERT 0 3"
  error?: string | null;
}

export interface ExecutionResult {
  execution_id: UUID;
  status: string;
  columns?: { name: string; type: string }[];
  rows?: unknown[][];
  row_count?: number | null;
  /** Present only for a script of several statements: every statement's own result. */
  results?: ExecutionResultSet[];
  outputs?: { output_type: string; payload: Record<string, unknown> }[];
  error_message?: string | null;
}

// Local, client-only state machine for the execution panel — driven by WS
// envelope `type` strings, not by the persisted ExecutionOut.status column.
export type ExecutionState =
  | "queued" | "running" | "succeeded" | "failed" | "cancelled";

// ---------- Codegen chat ----------
export type CodegenTarget = Schemas["CodegenSessionCreate"]["target"];
export type CodegenEngine = NonNullable<Schemas["CodegenSessionCreate"]["engine"]>;
export type CodegenRole = "user" | "assistant" | "tool";
export type CodegenValidationStatus = "passed" | "failed" | "skipped";
export type CodegenSession = Schemas["CodegenSessionOut"];
export type CodegenSessionCreate = Schemas["CodegenSessionCreate"];
// CodegenMessageOut's `content` is a bare array in OpenAPI (the Anthropic
// content-block union isn't expressed in Pydantic), so narrow it here.
export type CodegenMessage = Omit<Schemas["CodegenMessageOut"], "content" | "role" | "validation_status"> & {
  role: CodegenRole;
  content: { type: string; text?: string; [key: string]: unknown }[];
  validation_status: CodegenValidationStatus | null;
};
export type CodegenPromptRequest = Schemas["CodegenPromptRequest"];
export type CodegenPromptAccepted = Schemas["CodegenPromptAccepted"];
export type CodegenApplyRequest = Schemas["CodegenApplyRequest"];

// ---------- Database connections / catalog ----------
export type ConnectionType = Schemas["DatabaseConnectionCreate"]["connection_type"];
export type DatabaseConnection = Schemas["DatabaseConnectionOut"];
export type DatabaseConnectionCreate = Schemas["DatabaseConnectionCreate"];
export type ConnectionTestResult = Schemas["ConnectionTestResult"];
export type CatalogNamespace = Schemas["CatalogNamespace"];
export type CatalogTable = Schemas["CatalogTable"];
export type CatalogColumn = Schemas["CatalogColumn"];
/** GET /connections/ucs-catalogs — a Unified Catalog source a connection can link to. */
export type UcsCatalogOption = Schemas["UcsCatalogOption"];

// ---------- Settings ----------
export type EngineStatus = Schemas["EngineStatus"];
export type EngineState = EngineStatus["status"];
export type EnginesOut = Schemas["EnginesOut"];
/** Which agent answers the ⌘K assistant: the in-perimeter gateway, or OpenAI / Anthropic with the admin's key. */
export type CodegenAgentConfig = Schemas["CodegenAgentConfigOut"];
export type CodegenProvider = CodegenAgentConfig["provider"];
export type VerifyKeyRequest = Schemas["VerifyKeyIn"];
export type VerifyKeyResponse = Schemas["VerifyKeyOut"];
export type UpdateCodegenAgentRequest = Schemas["UpdateAgentIn"];

// ---------- FlinkSQL Runner (proxied at /api/v1/flink/*) ----------
export type FlinkJob = Schemas["FlinkJob"];
export type FlinkSinkEngine = FlinkJob["sink_engine"];
export type FlinkJobStatus = FlinkJob["status"];
export type FlinkJobCreate = JsonRequestBody<"/api/v1/flink/jobs", "post">;
export type FlinkJobUpdate = JsonRequestBody<"/api/v1/flink/jobs/{job_id}", "patch">;
// Registered from Git: the workspace service clones the repo, assembles the
// FlinkSQL and submits it (POST /api/v1/flink/jobs/from-git).
export type FlinkJobFromGit = Schemas["FlinkJobFromGit"];
export type FlinkJobSync = Schemas["FlinkJobSync"];
export type FlinkSqlValidate = Schemas["FlinkSqlValidate"];
export type FlinkDeployment = Schemas["FlinkDeployment"];
// `flink_job_id` is the Flink cluster's own JobID — what the Flink Web UI keys
// its pages on, distinct from our job's UUID.
export type FlinkJobLiveStatus = Schemas["FlinkJobLiveStatus"];
export type FlinkValidateResponse = Schemas["FlinkValidateResponse"];
export type FlinkUi = Schemas["FlinkUiOut"];

// ---------- dbt Runner (proxied at /api/v1/dbt/*) ----------
export type DbtProject = Schemas["DbtProject"];

// Alerts (Slack / Teams channels, failure rules, the alerts themselves)
export type Alert = Schemas["AlertOut"];
export type AlertDetail = Schemas["AlertDetail"];
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
export type AlertChannelTest = Schemas["ChannelTestResult"];
export type AlertSweep = Schemas["SweepOut"];
export type DbtEngine = DbtProject["engine"];
/** Most recent run, embedded in the project list (null = never built). */
export type DbtLastRun = Schemas["DbtLastRun"];
export type DbtRun = Schemas["DbtRun"];
export type DbtRunStatus = DbtRun["status"];
export type DbtRunTrigger = DbtRun["trigger"];
export type DbtRunEvent = Schemas["DbtRunEvent"];
export type DbtLineageEdge = Schemas["DbtLineageEdge"];
export type DbtLineage = Schemas["DbtLineage"];
export type DbtProjectRegister = JsonRequestBody<"/api/v1/dbt/projects/register", "post">;
export type DbtProjectUpdate = JsonRequestBody<"/api/v1/dbt/projects/{project_id}", "patch">;

// ---------- Data Quality (proxied at /api/v1/dq/*) ----------
// The Workspace UI owns only the job-facing half of DQ (bindings + render);
// rules and rulesets are authored in the Unified Catalog.
export type DqRule = Schemas["DqRule"];
export type DqDimension = DqRule["dimension"];
export type DqSeverity = DqRule["severity"];
export type DqRulesetSummary = Schemas["DqRulesetSummary"];
export type DqRulesetItem = Schemas["DqRulesetItem"];
export type DqRuleType = DqRule["rule_type"];
export type DqBinding = Schemas["DqBinding"];
export type DqJobType = DqBinding["job_type"];
export type DqEnforcementMode = DqBinding["enforcement_mode"];
export type DqBindingCreate = JsonRequestBody<"/api/v1/dq/bindings", "post">;
/** Only the artifact field(s) matching the binding's job_type are populated. */
export type DqRender = Schemas["DqRender"];
/** POST /dq/sync/ucs — read the counts, partial failure still answers 200. */
export type DqUcsSyncReport = Schemas["DqUcsSyncReport"];
/** POST /dq/bindings/{id}/apply — 200 even when nothing was pushed; read `applied`. */
export type DqApplyResult = Schemas["DqApplyResult"];
