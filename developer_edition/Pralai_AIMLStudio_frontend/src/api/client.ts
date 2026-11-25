import type {
  BaseModel,
  CatalogModel,
  Dataset,
  Endpoint,
  AlertPolicy,
  ActiveAlert,
  TrainingRun,
  TrainingRunDetail,
  TrainingRunRequest,
  RateLimits,
  ApiError,
  Pipeline,
  PipelineDetail,
  NotificationChannel,
  Deployment,
  PendingPromotion,
  ModelMonitoring,
  ModelOverviewCard,
  ModelExplainability,
  ModelInputSchema,
  ModelTimeseries,
  DatasetComment,
  DatasetAuditEntry,
  DatasetAccessGrant,
  DatasetSnapshot,
  TargetCandidates,
  DatasetUploadResult,
  ComputePool,
  AlertHistoryEntry,
  Experiment,
  ExperimentDetail,
  HpoSearch,
  HpoSearchDetail,
  HpoSearchRequest,
  ModelGovernance,
  TestPrediction,
  AlgorithmSpec,
  PipelineDefinition,
  WorkerFleet,
  PromotionDetail,
  ComputePoolsResponse,
  ClusterTelemetry,
} from "../types/domain";

export interface ArtifactLocation {
  uri: string; storage: string; endpoint: string | null; bucket: string; key: string; fileName: string;
  exists: boolean; sizeBytes: number | null; lastModified: string | null; consoleUrl: string | null; downloadPath: string;
}
import { apiUrl } from "../runtimeConfig";
// No identity headers are ever sent from the browser: Envoy sets them from the
// user's validated ticket (contract: docs/auth/01-CONTRACT.md).

class ApiRequestError extends Error {
  status: number;
  body: ApiError;
  constructor(status: number, body: ApiError) {
    super(body.error || `Request failed with status ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // apiUrl(), not a hardcoded "/api": mounted in the Control Portal the backend
  // is reached through the tunnel prefix the host passes in, and a root-relative
  // path would hit the HOST's origin instead of the data plane.
  const res = await fetch(apiUrl(path), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiRequestError(res.status, body as ApiError);
  return body as T;
}

/** What a channel form sends. webhookUrl is required to create; blank on an edit keeps the stored one. */
export interface ChannelInput {
  name: string;
  type: "slack" | "teams";
  enabled?: boolean;
  channelDest?: string;
  minSeverity?: "low" | "medium" | "high";
  modelId?: string | null;
}

/** Inclusive YYYY-MM-DD bounds; an empty side is open. */
export interface DateRange { from: string; to: string }
function rangeQuery(range?: DateRange): string {
  const q = new URLSearchParams();
  if (range?.from) q.set("from", range.from);
  if (range?.to) q.set("to", range.to);
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const api = {
  // ---- Dashboard aggregates (real queries; empty until real activity exists) ----
  getActivity: (limit = 8) =>
    request<{ actor: string; action: string; target: string; createdAt: string }[]>(
      `/dashboard/activity?limit=${limit}`
    ),
  getTrainingActivity: (days = 14) =>
    request<{
      days: number; hasData: boolean; total: number;
      series: { day: string; runs: number; succeeded: number; failed: number }[];
    }>(`/dashboard/training-activity?days=${days}`),
  getComputeSpend: (days = 7) =>
    request<{
      days: number; measured: string; hasData: boolean; totalHours: number;
      profiles: { id: string; label: string; kind: "cpu" | "gpu"; hours: number; runs: number; unclaimedRuns: number }[];
    }>(`/dashboard/compute-spend?days=${days}`),

  getBaseModels: (domain?: "ml" | "llm") =>
    request<BaseModel[]>(`/base-models${domain ? `?domain=${domain}` : ""}`),
  getBaseModel: (id: string) => request<BaseModel>(`/base-models/${id}`),

  // The hyperparameter surface the Train wizard renders and POST /training-runs
  // validates against — one source, so the form and the server can't drift.
  getAlgorithms: () => request<AlgorithmSpec[]>("/algorithms"),
  getAlgorithm: (key: string) => request<AlgorithmSpec>(`/algorithms/${encodeURIComponent(key)}`),

  // Ad-hoc test predictions. These go through the in-app test routes rather
  // than the public bearer-token serving endpoint, because the browser does
  // not hold an endpoint's client secret (it is stored hashed and shown once
  // at promotion). Same inference path, operator-authenticated instead of
  // API-client-authenticated — see backend routes/serve.ts. `dataSource` says
  // whether the answer came from real weights ("measured") or the
  // development-only deterministic stand-in ("stand-in"); the UI must show
  // that distinction rather than present both as a prediction.
  testPredict: (modelId: string, input: unknown) =>
    request<TestPrediction>(`/serve/${encodeURIComponent(modelId)}/test-predict`, {
      method: "POST",
      body: JSON.stringify(input),
    }),
  testPredictBaseModel: (id: string, input: unknown) =>
    request<TestPrediction>(`/base-models/${encodeURIComponent(id)}/test-predict`, {
      method: "POST",
      body: JSON.stringify(input),
    }),

  // Edit a registered model's description.
  updateModelDescription: (id: string, description: string) =>
    request<{ id: string; description: string | null }>(`/models/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ description }) }),
  getModels: () => request<CatalogModel[]>("/models"),
  getModelsOverview: () => request<ModelOverviewCard[]>("/models/overview"),
  getModel: (id: string) => request<CatalogModel>(`/models/${id}`),

  getDatasets: () => request<Dataset[]>("/datasets"),
  getDataset: (name: string) => request<Dataset>(`/datasets/${encodeURIComponent(name)}`),
  // Rename and/or change the description. A rename moves the dataset's files, comments,
  // access grants and training lineage to the new name.
  updateDataset: (name: string, body: { name?: string; description?: string }) =>
    request<Dataset>(`/datasets/${encodeURIComponent(name)}`, { method: "PATCH", body: JSON.stringify(body) }),
  // Only an empty dataset (no files) can be deleted — the server refuses otherwise.
  deleteDataset: (name: string) =>
    request<{ ok: true }>(`/datasets/${encodeURIComponent(name)}`, { method: "DELETE" }),
  createDataset: (req: {
    name: string; source: "upload" | "iceberg"; structureType: "structured" | "unstructured";
    visibility: "private" | "org"; tags: string[]; detail: string; description?: string;
    iceberg?: { catalog: string; schemaName: string; table: string; snapshot: string | null };
  }) => request<Dataset>("/datasets", { method: "POST", body: JSON.stringify(req) }),
  getDatasetSnapshots: (name: string) => request<DatasetSnapshot[]>(`/datasets/${encodeURIComponent(name)}/snapshots`),
  getDatasetSnapshot: (name: string, version: number) =>
    request<DatasetSnapshot>(`/datasets/${encodeURIComponent(name)}/snapshots/${version}`),
  // A browser-navigable URL, so it needs the runtime base too — embedded, a
  // root-relative path downloads from the HOST's origin, not the data plane.
  datasetSnapshotDownloadUrl: (name: string, version: number) =>
    apiUrl(`/datasets/${encodeURIComponent(name)}/snapshots/${version}/download`),
  getIcebergCatalogs: () => request<{ name: string; warehouse: string | null; configured: boolean; needsCredentials: boolean }[]>("/iceberg/catalogs"),
  // Reads the Iceberg table's current (or pinned) snapshot into a new Parquet version of the dataset.
  // credentials: storage keys typed in for this one pull; the server uses them once and keeps nothing.
  pullIcebergSnapshot: (name: string, table: string, snapshotId?: string | null, credentials?: { accessKeyId: string; secretAccessKey: string; sessionToken?: string }) =>
    request<{ ok: true; unchanged?: boolean; datasetSnapshotVersion: number; icebergSnapshotId: string; table: string; rowCount: number; columnNames: string[] }>(
      `/datasets/${encodeURIComponent(name)}/iceberg-snapshots`,
      { method: "POST", body: JSON.stringify({ table, ...(snapshotId ? { snapshotId } : {}), ...(credentials ? { credentials } : {}) }) }),
  deleteDatasetSnapshot: (name: string, version: number) =>
    request<{ ok: true }>(`/datasets/${encodeURIComponent(name)}/snapshots/${version}`, { method: "DELETE" }),
  // Real binary upload — bypasses the JSON `request` helper so the raw file
  // body reaches express.raw() intact (the backend computes checksum +
  // profiling on the exact bytes). format drives the ?format= query + content-type.
  uploadDataset: async (name: string, file: File, format: string): Promise<DatasetUploadResult> => {
    const CT: Record<string, string> = {
      csv: "text/csv", json: "application/json", jsonl: "application/x-ndjson",
      parquet: "application/octet-stream", xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "image-zip": "application/zip", pdf: "application/pdf",
      docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      txt: "text/plain", md: "text/markdown", html: "text/html",
      xls: "application/vnd.ms-excel", zip: "application/zip", ppt: "application/vnd.ms-powerpoint",
      pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      png: "image/png", wav: "audio/wav",
    };
    // The file's own name: same name = a new version of that file; new name = another file.
    const res = await fetch(apiUrl(`/datasets/${encodeURIComponent(name)}/upload?format=${encodeURIComponent(format)}&filename=${encodeURIComponent(file.name)}`), {
      method: "POST",
      headers: {
        "Content-Type": CT[format] || "application/octet-stream",
      },
      body: file,
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new ApiRequestError(res.status, body as ApiError);
    return body as DatasetUploadResult;
  },

  getEndpoints: () => request<Endpoint[]>("/endpoints"),
  getEndpoint: (modelId: string) => request<Endpoint>(`/endpoints/${modelId}`),
  updateRateLimits: (modelId: string, limits: RateLimits) =>
    request<Endpoint>(`/endpoints/${modelId}/rate-limits`, { method: "PATCH", body: JSON.stringify(limits) }),
  regenerateSecret: (modelId: string) =>
    request<{ clientSecret: string; clientSecretMasked: string }>(`/endpoints/${modelId}/regenerate-secret`, { method: "POST" }),

  getAlertPolicies: (range?: DateRange) => request<AlertPolicy[]>(`/alert-policies${rangeQuery(range)}`),
  createAlertPolicy: (rule: { name: string; signal: string; severity: "low" | "medium" | "high"; threshold: number; modelId: string | null; channelId: string | null }) =>
    request<AlertPolicy>("/alert-policies", { method: "POST", body: JSON.stringify(rule) }),
  deleteAlertPolicy: (id: string) => request<{ ok: true }>(`/alert-policies/${id}`, { method: "DELETE" }),
  updateAlertPolicy: (id: string, patch: { enabled?: boolean; defaultThreshold?: number; channelId?: string | null }) =>
    request<AlertPolicy>(`/alert-policies/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  setAlertOverride: (policyId: string, modelId: string, threshold: number | null) =>
    request<{ ok: true }>(`/alert-policies/${policyId}/override/${modelId}`, { method: "PATCH", body: JSON.stringify({ threshold }) }),
  // Runs one real evaluation pass now. Returns how many model/policy pairs it
  // could evaluate and how many it skipped for want of enough measured traces —
  // skipped is a real outcome, not a failure.
  evaluateAlertsNow: () =>
    request<{ evaluated: number; skippedInsufficientData: number; fired: string[] }>("/alerts/evaluate", { method: "POST" }),
  getActiveAlerts: (range?: DateRange) => request<ActiveAlert[]>(`/alerts/active${rangeQuery(range)}`),
  getAlertHistory: (range?: DateRange) => request<AlertHistoryEntry[]>(`/alerts/history${rangeQuery(range)}`),
  acknowledgeAlert: (modelId: string, policyId: string) =>
    request<{ ok: true }>(`/alerts/${modelId}/${policyId}/acknowledge`, { method: "POST" }),
  resolveAlert: (modelId: string, policyId: string) =>
    request<{ ok: true }>(`/alerts/${modelId}/${policyId}/resolve`, { method: "POST" }),

  getNotificationChannels: () => request<NotificationChannel[]>("/notification-channels"),
  createNotificationChannel: (ch: ChannelInput & { webhookUrl: string }) =>
    request<NotificationChannel>("/notification-channels", { method: "POST", body: JSON.stringify(ch) }),
  updateNotificationChannel: (id: string, patch: Partial<ChannelInput & { webhookUrl: string }>) =>
    request<NotificationChannel>(`/notification-channels/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteNotificationChannel: (id: string) =>
    request<{ ok: true; reassignedTo: string | null }>(`/notification-channels/${id}`, { method: "DELETE" }),

  /** Which columns can actually serve as a training target, graded by the same
   *  rule the train-time quality gate uses. */
  getTargetCandidates: (dataset: string, task: string, version?: number | null) =>
    request<TargetCandidates>(
      `/datasets/${encodeURIComponent(dataset)}/target-candidates?task=${encodeURIComponent(task)}` +
        (version ? `&version=${version}` : "")
    ),

  getTrainingRuns: () => request<TrainingRun[]>("/training-runs"),
  createTrainingRun: (req: TrainingRunRequest) =>
    request<TrainingRun>("/training-runs", { method: "POST", body: JSON.stringify(req) }),

  getPipelines: () => request<Pipeline[]>("/pipelines"),
  getPipeline: (id: string) => request<PipelineDetail>(`/pipelines/${id}`),
  restartPipeline: (id: string) => request<{ ok: true }>(`/pipelines/${id}/restart`, { method: "POST" }),
  getPipelineDefinitions: () => request<PipelineDefinition[]>("/pipeline-definitions"),
  /** Saving under an existing name creates the next version — definitions are immutable. */
  createPipelineDefinition: (body: { name: string; computeProfile: string; stages: PipelineDefinition["stages"] }) =>
    request<PipelineDefinition>("/pipeline-definitions", { method: "POST", body: JSON.stringify(body) }),
  runPipelineDefinition: (id: string) =>
    request<{ pipelineId: string }>(`/pipeline-definitions/${id}/run`, { method: "POST" }),

  // Fleet visibility straight out of queue state — see backend server.ts.
  getWorkers: () => request<WorkerFleet>("/workers"),
  /** Where a model artifact really lives: storage, bucket, path, existence, size. */
  locateArtifact: (uri: string) => request<ArtifactLocation>(`/artifacts/locate?uri=${encodeURIComponent(uri)}`),
  /** Fetch an artifact and save it under its own file name. */
  downloadArtifact: async (uri: string, fileName: string) => {
    const res = await fetch(apiUrl(`/artifacts/download?uri=${encodeURIComponent(uri)}`));
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `download failed (HTTP ${res.status})`);
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url; a.download = fileName; document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  },
  getTrainingRun: (id: string) => request<TrainingRun>(`/training-runs/${id}`),
  getTrainingRunDetail: (id: string) => request<TrainingRunDetail>(`/training-runs/${id}/detail`),
  /** A new run with this failed run's exact configuration and dataset snapshot. */
  retrainTrainingRun: (id: string) =>
    request<TrainingRun>(`/training-runs/${id}/retrain`, { method: "POST" }),
  cancelTrainingRun: (id: string) =>
    request<{ ok: true; status: string }>(`/training-runs/${id}/cancel`, { method: "POST" }),

  getDeployments: () => request<Deployment[]>("/deployments"),
  getPendingPromotions: () => request<PendingPromotion[]>("/promotions/pending"),
  getPromotionDetail: (modelId: string, version: number) =>
    request<PromotionDetail>(`/promotions/${encodeURIComponent(modelId)}/${version}`),
  promote: (modelId: string, version: number) =>
    request<{ ok: true; promotedTo: number; previousVersion: number | null }>(`/deployments/${modelId}/promote`, {
      method: "POST",
      body: JSON.stringify({ version }),
    }),
  rollback: (modelId: string) =>
    request<{ ok: true; rolledBackTo: number; rolledBackFrom: number }>(`/deployments/${modelId}/rollback`, { method: "POST" }),

  getModelMonitoring: (modelId: string) => request<ModelMonitoring>(`/models/${modelId}/monitoring`),
  getModelExplainability: (modelId: string) => request<ModelExplainability>(`/models/${modelId}/explainability`),
  getModelInputSchema: (modelId: string) => request<ModelInputSchema>(`/models/${modelId}/input-schema`),
  getModelTimeseries: (modelId: string, days = 30) => request<ModelTimeseries>(`/models/${modelId}/timeseries?days=${days}`),

  getDatasetComments: (name: string) => request<DatasetComment[]>(`/datasets/${encodeURIComponent(name)}/comments`),
  postDatasetComment: (name: string, body: string) =>
    request<DatasetComment>(`/datasets/${encodeURIComponent(name)}/comments`, { method: "POST", body: JSON.stringify({ body }) }),
  getDatasetAuditLog: (name: string) => request<DatasetAuditEntry[]>(`/datasets/${encodeURIComponent(name)}/audit-log`),
  getDatasetAccess: (name: string) => request<DatasetAccessGrant[]>(`/datasets/${encodeURIComponent(name)}/access`),
  grantDatasetAccess: (name: string, principal: string, levels: string[]) =>
    request<DatasetAccessGrant>(`/datasets/${encodeURIComponent(name)}/access`, { method: "POST", body: JSON.stringify({ principal, levels }) }),
  revokeDatasetAccess: (name: string, id: number) =>
    request<{ ok: true }>(`/datasets/${encodeURIComponent(name)}/access/${id}`, { method: "DELETE" }),

  completeTrainingRun: (id: string) => request<TrainingRun>(`/training-runs/${id}/complete`, { method: "POST" }),
  getExperiments: () => request<Experiment[]>("/experiments"),
  getExperiment: (id: string) => request<ExperimentDetail>(`/experiments/${id}`),
  createExperiment: (req: { name: string; objectiveMetric: string; objectiveDirection: "maximize" | "minimize"; team?: string; tags?: string[]; description?: string }) =>
    request<Experiment>("/experiments", { method: "POST", body: JSON.stringify(req) }),
  getHpoSearches: () => request<HpoSearch[]>("/hpo-searches"),
  getHpoSearch: (id: string) => request<HpoSearchDetail>(`/hpo-searches/${id}`),
  createHpoSearch: (req: HpoSearchRequest) => request<HpoSearchDetail>("/hpo-searches", { method: "POST", body: JSON.stringify(req) }),
  promoteBestTrial: (searchId: string, modelName: string) =>
    request<{ trainingRunId: string; modelName: string; promotedFromTrial: number; metricValue: number; hyperparams: Record<string, unknown>; registrationWarning?: string }>(
      `/hpo-searches/${searchId}/promote-best`,
      { method: "POST", body: JSON.stringify({ modelName }) }
    ),
  getComputePools: () => request<ComputePoolsResponse>("/compute-pools"),
  getClusterTelemetry: () => request<ClusterTelemetry>("/cluster/telemetry"),
  getModelGovernance: (modelId: string) => request<ModelGovernance>(`/models/${encodeURIComponent(modelId)}/governance`),
};

export { ApiRequestError };
