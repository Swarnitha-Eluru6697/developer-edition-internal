export interface FlowNode {
  id: string;
  type: string;
  x: number;
  y: number;
  sub?: string;
  config: Record<string, any>;
  parent_id?: string | null; // set when this node is nested inside a loop container
}

export interface FlowEdge {
  id: string;
  source: string;
  target: string;
  source_handle?: string | null; // "true" | "false" for a condition node's two outputs
}

export interface AgentInputVariable {
  name: string;
  type: 'string' | 'number' | 'boolean' | 'array' | 'object';
  description?: string;
}

export interface Agent {
  id: string;
  name: string;
  type: 'Data' | 'App';
  subtype: string;
  plane: 'Data' | 'App' | 'Snippet';
  owner: string;
  status: 'draft' | 'staged' | 'published';
  version: string;
  health_score: number | null;
  health_status: string;
  model: string;
  mode: 'agentic' | 'fixed';
  app_type?: 'workflow' | 'chatflow' | 'chatbot' | 'agent' | 'text_generator' | null;
  tags: string[];
  description: string;
  features?: AgentFeatures;
  updated_at: string;
  runs_7d: number;
  cost_7d: string;
  first_party: boolean;
  template_name?: string | null;
  template_id?: string | null;
  template_version_id?: string | null;
  flow: { nodes: FlowNode[]; edges: FlowEdge[] };
  // Real optimistic-concurrency revision (§0.71, GAP_REPORT) — bumped
  // by exactly 1 on every real flow mutation; the Builder sends this
  // back as a real If-Match header on its own next mutation so a
  // second, concurrently-open tab's stale write is rejected with a
  // real 409 instead of silently overwriting a change that already
  // landed. See api.ts's own addNode/moveNode/etc. ifMatch parameter.
  draft_revision: number;
  // Only meaningful for plane="Snippet" — real declared inputs a
  // snippet expects from whatever workflow it's inserted into.
  input_variables?: AgentInputVariable[];
  // Real, first-class MCP endpoint credential (§0.101, GAP_REPORT) —
  // these now live on real, top-level Agent fields, never inside
  // features["mcp"] — a version restore can no longer resurrect a
  // rotated/revoked token, since restore only ever touches
  // flow/app_type/features, never these. mcp_token_encrypted is
  // always masked in a real response (never the actual, plaintext
  // credential) — the real plaintext url is only ever available
  // just-in-time from an actual enable/regenerate API response.
  mcp_enabled?: boolean;
  mcp_token_encrypted?: string | null;
  metrics: Record<string, string>;
  ucs_synced?: boolean;
  ucs_sync_error?: string | null;
  ucs_fqn?: string | null;
}

export interface AgentLog {
  id: number;
  agent_id: string;
  ts: string;
  level: 'INFO' | 'WARN' | 'ERROR';
  msg: string;
  trace_id: string | null;
}

export interface AgentVersion {
  id: number;
  agent_id: string;
  version: string;
  flow: { nodes: FlowNode[]; edges: FlowEdge[] };
  app_type: string | null;
  features: Record<string, any>;
  entitlement_plan: Record<string, any>;
  published_by: string;
  created_at: string;
  is_current: boolean;
}

export interface SavedResult {
  id: number;
  agent_id: string;
  invoking_user: string;
  input: string;
  output: string | null;
  structured_output: Record<string, string> | null;
  created_at: string;
}

export interface Run {
  id: string;
  agent_id: string;
  agent_name: string;
  status: 'success' | 'failed' | 'denied' | 'running' | 'paused' | 'blocked';
  duration: string;
  cost: string;
  hops: number;
  ts: string;
  fail_class: 'orchestration' | 'harness' | 'ucs' | 'moderation' | null;
}

export interface Trace {
  id: string;
  agent_id: string;
  agent_name: string;
  version: string;
  principal: string;
  plane: 'Data' | 'App';
  spans: number;
  duration: string;
  ts: string;
  status: 'ok' | 'ucs_denial' | 'harness_fail' | 'orch_fail' | 'paused' | 'moderation_blocked';
  user_input?: string;
  final_output?: string | null;
}

export interface TraceSpan { name: string; kind: string; duration_ms: number; note?: string | null; input_preview?: string | null; output_preview?: string | null }
export interface TraceSpansResponse { trace_id: string; fail_class: string | null; spans: TraceSpan[]; user_input?: string; final_output?: string | null }

export interface AlertHistoryEntry { action: string; by: string; ts: string }
export interface Alert {
  id: string;
  sev: 'high' | 'medium' | 'low';
  rule: string;
  agent_id: string;
  agent_name: string;
  state: 'open' | 'resolved';
  acked: boolean;
  ts: string;
  summary: string;
  history: AlertHistoryEntry[];
}

// A Slack or Teams channel alerts are sent to. `webhook_url` is
// deliberately absent: it is a credential the API never returns, so the
// UI recognises a destination by `url_hint` and knows one is set from
// `configured`.
// A condition an alert rule found true. `already_open` says whether a
// sweep would actually raise it or suppress it as a duplicate.
// Lineage and quality are computed by this platform from its own runs
// and eval results, then published to UCS. They are not read back from
// the catalogue: UCS only knows what it has been told, so asking it
// showed an empty graph and a zero score for agents we hold full
// history for.
export interface AgentLineage {
  agent_id: string;
  agent_name: string;
  fqn: string;
  upstream: { id: string; name: string; fqn: string }[];
  downstream: { id: string; name: string; fqn: string }[];
  runs: { id: string; status: string; ts: string | null }[];
}
export interface AgentQualityDim {
  verdict: 'pass' | 'warn' | 'fail' | 'unknown';
  observed: number | null;
  threshold: number | null;
  detail: string;
  rows_scanned: number;
}
export interface AgentQuality {
  agent_id: string;
  agent_name: string;
  fqn: string;
  score: number | null;
  status: 'pass' | 'warn' | 'fail' | 'unknown';
  dims: Record<string, AgentQualityDim>;
  window_days: number;
  computed_at: string;
}
export interface AgentLiveJob {
  id: string;
  status: string;
  started_at: string | null;
  running_for_s: number | null;
  invoking_user: string | null;
  workflow_id: string | null;
}

export interface CatalogPublishResult {
  fqn: string; sent: string[]; failed: string[];
  attributes?: { status?: string; error?: string };
}

export interface AlertCondition {
  sev: 'high' | 'medium' | 'low';
  rule: string;
  agent_id: string;
  agent_name: string;
  summary: string;
  already_open: boolean;
}
export interface AlertRule {
  id: number;
  name: string;
  kind: string;
  severity: 'high' | 'medium' | 'low';
  threshold: number;
  window_seconds: number;
  min_runs: number;
  fail_classes: string[];
  agent_id: string | null;
  agent_name: string | null;
  enabled: boolean;
}
export interface AlertRuleIn {
  name: string;
  kind: string;
  severity: 'high' | 'medium' | 'low';
  threshold: number;
  window_seconds: number;
  min_runs: number;
  fail_classes: string[];
  agent_id: string | null;
  enabled: boolean;
}
// What each kind does and what its threshold means — the meaning of
// `threshold` depends on the kind, so the UI shows it beside the field.
export interface AlertRuleKind {
  value: string; label: string; threshold: string; desc: string;
}

export interface AlertRulePreview {
  conditions: AlertCondition[];
  thresholds: Record<string, number>;
}
export interface AlertSweepResult {
  evaluated: string; raised: string; suppressed_as_duplicate: string;
  deliveries: string; destinations: string; resolved: string;
}

export interface AlertDestination {
  id: number;
  name: string;
  kind: 'slack' | 'teams';
  enabled: boolean;
  min_severity: 'low' | 'medium' | 'high';
  agent_id: string | null;
  url_hint: string;
  configured: boolean;
  last_attempt_at: string | null;
  last_status: 'ok' | 'failed' | null;
  last_detail: string | null;
}
export interface AlertDestinationIn {
  name: string;
  kind: 'slack' | 'teams';
  webhook_url?: string;
  enabled: boolean;
  min_severity: 'low' | 'medium' | 'high';
  agent_id: string | null;
}

export interface Role { id: number; agent_id: string; agent_name: string; tier: string; group: string }

export interface ModelProvider {
  name: string;
  version: string;
  status: 'credits' | 'needs_key';
  types: string[];
  api_key_configured: boolean;
  // §0.114, GAP_REPORT — a real, computed field (never a stored,
  // hand-maintained flag) reflecting whether model_adapter.py's own
  // real provider adapters (_call_anthropic/_call_openai) genuinely
  // execute Agent runs for this provider, or whether it's still a
  // real, honest catalog entry with no actual runtime behind it yet.
  runtime_supported: boolean;
  // Whether the stored key can actually be DECRYPTED, not merely
  // whether one was saved. false means a key is on file but was
  // encrypted under a different SECRETS_ENCRYPTION_KEY, so every
  // call using it fails while the row still looks configured.
  // null when nothing is stored to test.
  key_usable?: boolean | null;
  unsupported_reason?: string | null;
  requires_base_url?: boolean;
  api_base_url?: string | null;
  organization?: string | null;
}

export interface Tool { name: string; org: string; version: string | null; desc: string; source: 'marketplace' | 'builtin' }
export interface ToolActionParam { name: string; type: string; required: boolean; desc: string }
export interface ToolAction { id: number; tool_name: string; name: string; desc: string; params: ToolActionParam[] }

export interface MCPServer { id: string; name: string; identifier: string; url: string; status: string; actions: number }

export interface DataSource {
  name: string; version: string; status: 'needs_auth' | 'connected'; auth: 'oauth' | 'apikey';
  // Secrets arrive masked as "••••••••"; sending the mask back on a
  // re-save keeps the stored value rather than overwriting it.
  config?: Record<string, any>;
  // For the Google sources: the identity the service-account key
  // authenticates as. Not a secret, and the one thing needed to share
  // a Drive folder with this platform — the key it comes from is
  // masked, so without this the address is unreachable from the UI.
  service_account_email?: string;
  // The numeric OAuth client id of the same key. Domain-wide delegation
  // in the Workspace Admin console asks for this and will not accept
  // the address — and delegation is the only route when the domain
  // blocks sharing with an outside address.
  service_account_client_id?: string;
  // `status` alone used to say "connected" with no credential stored,
  // so the Documents tab keys its dropdown off `browsable` instead.
  // Whether the stored secrets can actually be DECRYPTED. false means
  // they exist and the row looks connected, but every browse fails.
  credentials_usable?: boolean | null;
  browsable?: boolean;
  browse_supported?: boolean;
}

export interface DataSourceEntry {
  id: string; name: string; is_dir: boolean; path: string;
  size: number | null; mime: string | null; ingestible: boolean; note: string;
}

export interface DataSourceBrowseResult {
  source: string; path: string; entries: DataSourceEntry[];
  // Set when the listing would mislead without explanation — e.g. the
  // OneDrive root shows every drive the app was consented to, because
  // app-only auth has no signed-in user to scope to.
  notice?: string | null;
}

export interface SourceIngestResult {
  source: string; requested: number; ingested: number; failed: number;
  // Folders whose file cap was reached — reported rather than quietly
  // indexing an arbitrary subset of a large tree.
  truncated_folders?: string[];
  folder_file_cap?: number | null;
  results: { file_id: string; filename?: string; success: boolean; error?: string;
             document_id?: string; chunks_inserted?: number; embed_model?: string;
             table?: string;
             // Per file: in a batch, one file can be kept and the next
             // refused, so this is not a batch-level fact.
             original_file?: StoredOriginalFile | null;
             original_file_error?: string | null }[];
}

/** Published visibility of a reusable template. */
export type TemplateSource = 'platform' | 'private';
export type TemplateStatus = 'draft' | 'published' | 'archived';
export type TemplateOrigin = 'release' | 'organization' | 'private';

export const TEMPLATE_CATEGORIES = ['Sales', 'Service', 'Operations', 'Marketing', 'Data', 'General'] as const;
export type TemplateCategory = typeof TEMPLATE_CATEGORIES[number];

export interface TemplateRequirements {
  node_types: string[]; knowledge_bases: string[]; catalog_objects: string[]; tools: string[];
  models: string[]; env_vars: string[];
  review: { node: string; type: string; why: string }[];
}

export interface Template {
  id: string;
  key: string;
  name: string;
  type: 'Data' | 'App';
  app_type?: 'workflow' | 'chatflow' | 'chatbot' | 'agent' | 'text_generator' | null;
  desc: string;
  first_party: boolean;
  flow: { nodes: FlowNode[]; edges: FlowEdge[] };
  source: TemplateSource;
  origin: TemplateOrigin;
  status: TemplateStatus;
  version: string;
  category?: TemplateCategory | null;
  vertical?: string | null;
  tags: string[];
  requirements: TemplateRequirements;
  owner?: string | null;
  release_managed: boolean;
  hidden: boolean;
  draft_revision: number;
  published_revision?: number | null;
  current_version_id?: string | null;
  published_at?: string | null;
  published_by?: string | null;
  has_unpublished_changes: boolean;
  created_at: string;
  updated_at: string;
}

export interface TemplateVersion {
  id: string; template_id: string; version: string; name: string; type: 'Data' | 'App';
  app_type?: Template['app_type']; desc: string; first_party: boolean;
  flow: Template['flow']; category?: TemplateCategory | null; vertical?: string | null; tags: string[];
  requirements: TemplateRequirements; published_at: string; published_by?: string | null;
}

export interface SaveTemplateResult {
  template: Template;
  redactions: string[];
  issues: { severity: string; message: string; node_id: string | null }[];
}

export interface PublishTemplateResult {
  template: Template;
  version: TemplateVersion;
  issues: { severity: string; message: string; node_id: string | null }[];
}

export interface Eval { agent: string; set: string; passed: string; rate: string; drift: string; ts: string;
  // Identifiers, so a row in the Monitoring table can open the set
  // it names instead of being dead text.
  set_id?: string; agent_id?: string; run_id?: string }
export interface EvalHealth {
  avg_pass_rate: number | null;
  trend_delta_pts: number | null;
  trend_note: string | null;
  drift_flags: { agent_name: string; set_name: string; drift: string }[];
}

export interface EvalSet { id: string; name: string; agent_id: string; created_at: string; case_count?: number }
export interface EvalCase { id: string; eval_set_id: string; input: string; expected_output: string; criteria: string | null }
export interface EvalResultRow { id: number; eval_run_id: string; eval_case_id: string; actual_output: string | null; passed: boolean; score: number; reason: string }
export interface AgentRolePermission { key: string; summary: string; enforced: boolean }
export interface AgentRoleDef {
  name: string; label: string; summary: string;
  inherits: string | null; ucs_access_level: string;
  permissions: AgentRolePermission[];
}

export interface ChatHistoryEntry { role: 'user' | 'assistant'; content: string;
  conversation_id?: string | null; created_at: string | null }
export interface ConversationSummary { conversation_id: string; message_count: number;
  started_at: string | null; last_at: string | null; title: string | null }

export interface EvalRunRow { id: string; eval_set_id: string; agent_id: string; status: string;
  started_at: string; completed_at: string | null; pass_count: number; total_count: number; drift: string;
  // The published version each case was scored against (§0.93/§0.99):
  // two runs of the same set are only comparable when these match.
  agent_version?: string | null; agent_version_id?: number | null; flow_hash?: string | null }

export interface HomeStats {
  agents: number; runsToday: number; successRate: number | null; avgCost: string | null;
  /** Delivered and not yet acknowledged — the Alerts page's Open tab. The one number the
   *  Overview card and the sidebar badge both show. */
  openAlerts: number;
  acknowledgedAlerts?: number;
  undeliveredAlerts?: number;
}

export interface EnvVar {
  id: number;
  agent_id: string;
  key: string;
  type: 'string' | 'number' | 'secret';
  description: string;
  value: string; // masked ("••••••••") for type=secret except immediately after create/update
  created_at: string;
}

export type ChunkStructure = 'general' | 'parent_child' | 'qa';
export type IndexMethod = 'high_quality' | 'economical';
export type RetrievalMethod = 'vector' | 'fulltext' | 'hybrid';

export interface KnowledgeBase {
  id: string;
  name: string;
  provider: 'pgvector' | 'pinecone' | 'weaviate' | 'qdrant' | 'milvus' | 'other';
  config: Record<string, any> & {
    description?: string;
    icon?: string;
    chunk_structure?: ChunkStructure;
    index_method?: IndexMethod;
    keyword_count?: number;
    retrieval_method?: RetrievalMethod;
    reranking_enabled?: boolean;
    rerank_model?: string;
    semantic_weight?: number;
    keyword_weight?: number;
    top_k?: number;
    score_threshold_enabled?: boolean;
    score_threshold?: number;
    retrieval_api_enabled?: boolean;
    retrieval_api_key?: string;
  };
  status: 'untested' | 'connected' | 'failed';
  last_error: string | null;
  last_checked_at: string | null;
  // Whether this pipeline's stored secrets can be DECRYPTED, not merely
  // whether they exist. false means it looks connected and shows a
  // masked password, while every ingest and query fails.
  secrets_usable?: boolean | null;
  // Reported by the backend from the real ingest dispatch, not inferred
  // from the provider name here — a hardcoded provider test is exactly
  // what left the RAG page claiming Weaviate wasn't wired after it was.
  ingest_supported?: boolean;
  supports_fulltext?: boolean;
  created_at: string;
}

export interface KBSettings {
  description?: string;
  icon?: string;
  chunk_structure?: ChunkStructure;
  index_method?: IndexMethod;
  keyword_count?: number;
  retrieval_method?: RetrievalMethod;
  reranking_enabled?: boolean;
  // Which model does the reranking, by registered model name.
  // Safe to change any time: reranking is per-query and touches
  // no stored vector, so nothing needs re-ingesting.
  rerank_model?: string;
  semantic_weight?: number;
  keyword_weight?: number;
  top_k?: number;
  score_threshold_enabled?: boolean;
  score_threshold?: number;
  retrieval_api_enabled?: boolean;
  upload_api_enabled?: boolean;
  embedding_model?: string;
}

export interface KBRetrieveResult {
  id: number;
  content: string;
  source_filename: string;
  document_id: string | null;
  score: number;
  metadata: Record<string, any>;
}

export interface KBIngestResult {
  success: boolean;
  table: string;
  text_column: string;
  chunks_inserted: number;
  embed_model: string;
  embed_dim: number;
  source_filename: string;
  document_id?: string;
  // Present only when the file was audio: the text that got chunked
  // and embedded is a transcript, and this says which provider and
  // model produced it.
  transcription?: { provider: string; model: string; characters: number } | null;
  // The uploaded file itself, when it was kept rather than discarded
  // after its text was extracted.
  original_file?: StoredOriginalFile | null;
  // Set when keeping it was asked for and did not work — the document
  // still ingested, so this is reported rather than thrown.
  original_file_error?: string | null;
}

/** One place uploaded files can be kept. `env` is the server's own
 *  .env configuration, which the UI can select but not edit. */
export type GitProvider = 'github' | 'gitlab' | 'bitbucket' | 'azure_devops';

/** A GitHub / GitLab / Bitbucket account agents are committed to. The
 *  token never comes back — `token` is a mask when one is stored. */
export interface GitConnection {
  id: string;
  name: string;
  provider: GitProvider;
  provider_label: string;
  base_url: string | null;
  username: string | null;
  account: string | null;
  token: string | null;
  default_repo: string | null;
  default_branch: string | null;
  default_folder: string;
  commit_on_download: boolean;
  created_at: string | null;
  last_checked_at: string | null;
  last_error: string | null;
  web_url: string;
}

export interface GitRepo { full_name: string; default_branch: string; private: boolean; url?: string | null }

export interface GitCommitResult {
  created: boolean;
  commit_sha: string | null;
  commit_url: string | null;
  file_url: string | null;
  connection_id: string;
  connection: string;
  provider: GitProvider;
  repo: string;
  branch: string;
  path: string;
}

export interface StorageDestination {
  id: string;
  name: string;
  provider: string;
  bucket: string | null;
  region: string | null;
  endpoint_url: string | null;
  configured: boolean;
  label: string;
  editable: boolean;
  is_default: boolean;
  credentials?: string;
  last_checked_at?: string | null;
  last_error?: string | null;
}

export interface StorageTarget {
  backend: string;
  configured: boolean;
  label: string;
  bucket: string | null;
  endpoint_url: string | null;
  retention_days: number;
  /** The destination new uploads go to right now. */
  destination_id?: string | null;
  destinations?: StorageDestination[];
  /** False when the server is on a throwaway encryption key, so
   *  credentials entered now would be unreadable after a restart. */
  can_store_credentials?: boolean;
}

export interface StoredOriginalFile {
  id: string;
  filename: string;
  size: number;
  content_type: string;
  storage_backend: 's3' | 'local' | string;
  object_key: string;
  url: string;
  expires_at?: string | null;
}

export interface KnowledgeDocument {
  id: string;
  kb_id: string;
  filename: string;
  chunking_mode: string;
  max_words: number;
  overlap_words: number;
  word_count: number;
  chunk_count: number;
  retrieval_count: number;
  status: 'enabled' | 'disabled';
  created_at: string;
  metadata?: Record<string, any>;
  // Present when the original upload was kept, so the Documents tab
  // can offer it back rather than an ingested document being a
  // one-way trip from a file nobody can retrieve.
  original_file?: StoredOriginalFile | null;
}

export interface KnowledgeChunk {
  id: number;
  content: string;
  chunk_index: number;
  word_count: number;
  enabled: boolean;
  metadata: Record<string, any>;
}

export interface KnowledgeMetadataField {
  id: string;
  kb_id: string;
  name: string;
  type: 'string' | 'number' | 'time';
}

export interface KnowledgeRetrievalTestResult {
  id: number;
  content: string;
  source_filename: string;
  document_id: string | null;
  metadata: Record<string, any>;
  score: number;
}

export interface KnowledgeRetrievalTestHistoryEntry {
  id: number;
  kb_id: string;
  query: string;
  result_count: number;
  // Snapshot of what this run returned. Empty for rows recorded before
  // the snapshot column existed — compare against result_count to tell
  // that apart from a run that genuinely matched nothing.
  results: KnowledgeRetrievalTestResult[];
  created_at: string;
}

export interface HitlField {
  id?: string;
  name: string;
  type: 'paragraph' | 'select' | 'single_file' | 'file_list';
  options?: string[];
  default_value?: string;
  required?: boolean;
}

export interface HitlAction {
  id: string;
  title: string;
  style: 'primary' | 'secondary';
}

export interface HitlPromptState {
  requestId: string;
  renderedContent: string;
  fields: HitlField[];
  actions: HitlAction[];
  deliveryStatus?: Record<string, string>;
  values: Record<string, string>;
  resolved?: boolean;
}

// §0.117, GAP_REPORT — the real, structured shape a genuinely
// unexpected agent-execution failure's own error field can now be, in
// a real production deployment (see error_responses.py's own module
// docstring) — a real, deliberate replacement for a raw exception
// string, giving this frontend real, stable error semantics instead
// of parsing arbitrary text.
export interface SanitizedErrorInfo { error_code: string; correlation_id: string }

export interface BatchRunRowResult {
  row_index: number;
  inputs: Record<string, string>;
  status: string;
  output?: string | null;
  structured_output?: Record<string, string> | null;
  fail_class?: string | null;
  error?: string | SanitizedErrorInfo | null;
  duration_s?: number;
}

export interface BatchRunResult {
  total: number;
  succeeded: number;
  failed: number;
  paused: number;
  results: BatchRunRowResult[];
}

export interface WebAppSettings {
  display_name?: string;
  description?: string;
  icon?: string;
  theme?: 'light' | 'dark';
  color?: string;
  input_placeholder?: string;
  copyright?: string;
  privacy_policy_url?: string;
  terms_of_service_url?: string;
}

export interface AgentFeatures {
  conversation_opener?: { enabled: boolean; text: string; opening_questions?: string[] };
  chat_history?: { enabled: boolean; max_turns?: number };
  require_authenticated_user?: boolean;
  follow_up?: { enabled: boolean; prompt_mode?: 'default' | 'custom'; custom_prompt?: string; model?: string };
  citations?: { enabled: boolean };
  moderation?: {
    enabled: boolean;
    provider?: 'keywords' | 'ai' | 'custom_endpoint';
    input_enabled?: boolean;
    output_enabled?: boolean;
    input_preset_reply?: string;
    output_preset_reply?: string;
    keywords?: string[];
    custom_endpoint_url?: string;
  };
  publish?: { web_app?: boolean; api?: boolean };
  webapp_settings?: WebAppSettings;
}

/** GET /api/me — who the gateway says this user is, and what they may do. */
export interface Me {
  user: string | null;
  userId: string | null;
  tenant: string | null;
  permissions: string[];
}

/** A model deployed in ML Studio, as its GET /api/deployments returns it
 *  (read through the /mlstudio-api route). deployedBy is the gateway user
 *  name; null for deployments made before ML Studio recorded who deployed. */
export interface MlStudioDeployment {
  modelId: string;
  task: string;
  domain: 'ml' | 'llm';
  framework: string;
  currentVersion: number;
  previousVersion: number | null;
  healthStatus: 'healthy' | 'degraded';
  runtime: string;
  replicas: string;
  canRollback: boolean;
  deployedBy: string | null;
  deployedAt: string | null;
  endpointUrl: string | null;
  endpointStatus: string | null;
}

// Real, read-only runtime configuration (§0.45, GAP_REPORT) — exactly
// what the backend's own /api/settings/runtime-config endpoint returns.
// Never includes an actual secret value, only whether one is configured.
export interface RuntimeConfig {
  database: {
    pool_size_configured: number;
    max_overflow_configured: number;
    pool_timeout_seconds: number;
    pool_size_live: number;
    connections_checked_out_live: number;
    overflow_connections_in_use_live: number;
  };
  temporal: { address: string; task_queue: string };
  ucs: {
    base_url: string; tenant: string; agent_catalog_per_agent: boolean;
    agent_catalog_fallback: string; agent_schema: string;
    soft_check_mode: boolean; admin_token_configured: boolean;
  };
  platform: { base_url: string };
  model_provider: { anthropic_api_key_configured: boolean };
}

// Real Guardrails overview (§0.49, GAP_REPORT) — exactly what
// /api/guardrails/overview returns.
export interface GuardrailAgentRow {
  id: string;
  name: string;
  status: string;
  app_type: string | null;
  moderation: {
    enabled: boolean;
    provider: 'keywords' | 'custom_endpoint' | 'ai' | null;
    input_enabled: boolean;
    output_enabled: boolean;
    // Whether the config can actually block anything. `enabled` alone
    // cannot: with no provider the check never matches.
    effective: boolean;
    reason: string;
  };
  require_authenticated_user: boolean;
  ucs: { touches_checked_objects: boolean; checked_object_count: number };
  safety_caps: {
    llm_agent_node_count: number;
    llm_agent_nodes_with_custom_step_limit: number;
    loop_node_count: number;
    loop_nodes_with_custom_max_loops: number;
  };
}

export interface GuardrailsOverview {
  agents: GuardrailAgentRow[];
  platform_constants: {
    max_graph_steps: number;
    sub_agent_hop_cap: number;
    step_limit_default: number;
    max_loops_default: number;
    code_sandbox_timeout_seconds: number;
    code_sandbox_memory_mb: number;
    chat_history_max_turns: number;
  };
}


// One row of the Builder's model catalog (GET /api/integrations/llm-models):
// the built-in models plus everything registered on the Integrations page.
// `available` is reported rather than used to hide a row — a configured
// model that silently does not appear, with nothing explaining why, is the
// complaint this catalog exists to answer.
export interface LlmModelOption {
  name: string;
  provider: string | null;
  api_model_id: string | null;
  builtin: boolean;
  available: boolean;
  inferred: boolean;
  types: string[];
  note: string | null;
  supports_chat?: boolean;
  supports_tool_calling?: boolean;
  supports_vision?: boolean;
  /** A provider from Integrations with no models yet — shown, never selectable. */
  placeholder?: boolean;
  /** Set for a model deployed in ML Studio (name is `mlstudio/<model_id>`). */
  mlstudio?: { model_id: string; domain: 'ml' | 'llm' | string; task: string | null;
               version: number | null; deployed_by: string | null };
}

/** Where an asset is in use: draft nodes (ids) and saved/published versions (count). */
export interface AssetUsage { draft_nodes: string[]; versions: number }
/** A skill package in an agent's library (Agent node → Skills). */
export interface AgentSkillRef { id: string; name: string; description: string; size_bytes: number; file_count: number; created_at: string; used_by?: AssetUsage; retired?: boolean }
/** A reference file in an agent's library (Agent node → Files).
 *  status: ready = text is given to the model; stored_only = kept but not sent; failed = extraction found nothing. */
export interface AgentFileRef {
  id: string; filename: string; mime: string; kind: 'document' | 'image' | 'audio' | 'video'; size_bytes: number;
  status: 'ready' | 'stored_only' | 'failed'; status_detail: string; has_text: boolean; created_at: string; used_by?: AssetUsage; retired?: boolean;
  /** What "Save as template" does with this file. */
  template_policy?: TemplatePolicy;
}
export type TemplatePolicy = 'require' | 'embed' | 'exclude';
/** A file a template needs the installer to supply (Agent node config: files_required). */
export interface FileRequirement {
  requirement_id?: string; name?: string; filename: string; kind: string; accepted_types?: string[]; required?: boolean; size_bytes?: number;
}
export interface AgentAssetLimits {
  limits_bytes: Record<string, number>; skill_max_bytes: number; max_skills?: number; max_files?: number; max_library_bytes?: number;
  max_historical_bytes?: number; usage?: { live_bytes: number; historical_bytes: number; total_bytes: number };
}
export interface NodeEnvResult { entries: NodeSecretKey[]; draft_revision: number; agent: Agent }
export interface BindRequirementResult { files: string[]; files_required: FileRequirement[]; draft_revision: number; agent: Agent }
/** One row of the Agent node's Tools picker, as the backend reports it. */
export interface AgentToolEntry {
  value: string; label: string; group: string; source_type: 'tool' | 'mcp' | 'workflow'; desc?: string;
  callable: boolean; reason: string | null; params?: unknown[];
}
export interface NodeSecretKey { key: string; configured: boolean }
