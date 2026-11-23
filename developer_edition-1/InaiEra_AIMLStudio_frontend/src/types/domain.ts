// Mirrors backend/src/types/domain.ts. Kept as two files (not one shared
// package) to keep this scaffold simple to run — see README for the tradeoff.

export type Domain = "ml" | "llm";
// Matches the backend CHECK constraint on catalog_models.kind. Note this is
// NOT "base" — a base library entry is a BaseModel, a different table; every
// CatalogModel is something training produced.
export type ModelKind = "trained" | "fine-tuned" | "adapter";

export interface BaseModel {
  id: string;
  domain: Domain;
  title: string;
  task: string;
  description: string;
  framework: string;
  license: string;
  uri: string;
  gpu: boolean;
  compute_profile: string;
  long_description: string;
  // Coarse grouping for classical-ML entries; null for LLM entries, which are
  // grouped by `task` instead.
  category: string | null;
  algorithm_key: string | null;
  algorithm_variant: string | null;
  // Published model-card facts. Null where the model publishes no figure.
  params: string | null;
  context_window: string | null;
  alternatives: string[];
  // Null where this entry's trainer defines no documented default set — the UI
  // says so rather than rendering an invented hyperparameter block.
  default_hp: Record<string, unknown> | null;
  supported_hp: Record<string, unknown> | null;
  // Set only once real weights have been fetched into object storage. Until
  // then there is genuinely nothing to run inference against.
  stored_weights_uri?: string | null;
  weights_downloaded_at?: string | null;
}

export interface ModelVersion {
  v: number;
  alias: "champion" | "challenger" | "staging" | "archived" | "training";
  note: string;
}

export interface CatalogModel {
  id: string;
  /** What this trained model is for — written by the user, editable in the catalog. */
  description?: string | null;
  task: string;
  domain: Domain;
  kind: ModelKind;
  framework: string;
  base_ref: string | null;
  updated: string;
  versions: ModelVersion[];
  /** A training run for this model is queued or running right now. */
  training: boolean;
  /** The in-flight run, when `training` is true. */
  trainingRunId: string | null;
  trainingStatus: "queued" | "running" | null;
  /** Click-through target: the in-flight run, else the newest run that made
   *  this model. Null for entries that were seeded rather than trained here. */
  latestRunId: string | null;
  /** True for a model whose FIRST run is still going — it has no catalog row
   *  yet, and exists in this list only because that run does. */
  pending: boolean;
  /** Something is genuinely serving this model: a deployment row exists, or a
   *  version has been promoted to champion. Registration alone does not set it. */
  deployed: boolean;
  deployedVersion: number | null;
}

export interface Dataset {
  name: string;
  source: "upload" | "iceberg";
  structure_type: "structured" | "unstructured";
  size: string;
  rows: string;
  owner: string;
  tags: string[];
  detail: string;
  /** Written by the user when creating or editing the dataset. */
  description?: string | null;
  // Set once at least one real snapshot exists (the upload path advances these).
  storage_uri?: string | null;
  column_names?: string[] | null;
  latest_snapshot_version?: number | null;
  // Enriched display fields returned by the list/detail endpoints.
  visibility?: string;
  createdAt?: string;
  updatedAt?: string;
  deployTarget?: string;
  icebergRef?: { catalog: string; schemaName: string; table: string; snapshot: string | null } | null;
  files?: number;
}

export interface ProfileColumn {
  name: string;
  dtype: string;
  kind: "numeric" | "categorical";
  missingCount: number;
  missingPct: number;
  // numeric
  mean?: number | null;
  std?: number | null;
  min?: number | null;
  p25?: number | null;
  median?: number | null;
  p75?: number | null;
  max?: number | null;
  zeroCount?: number;
  outlierCount?: number;
  outlierPct?: number;
  isConstant?: boolean;
  // categorical
  cardinality?: number;
  cardinalityRatio?: number;
  isNearConstant?: boolean;
  topValues?: { value: string; count: number }[];
  allValueCounts?: { value: string; count: number }[] | null;
  histogram?: number[];
  leakageSignal?: number | { kind: string; value: number };
}

export interface DatasetProfile {
  ok: boolean;
  rowCount: number;
  columnCount: number;
  duplicateRowCount: number;
  columns: ProfileColumn[];
  sampleColumns?: string[];
  sampleRows?: Record<string, unknown>[];
}

export interface DatasetSnapshot {
  /** The uploaded file's name; uploads with the same name are versions of one file. */
  fileName?: string;
  /** This upload's version within its file (1, 2, …). */
  fileVersion?: number;
  /** True for the newest version of its file — the one training uses. */
  isLatest?: boolean;
  /** File counts by extension, for archives (from the upload profile). */
  fileTypes?: Record<string, number>;
  id: number;
  datasetName: string;
  version: number;
  storageUri: string;
  format: string;
  rowCount: number;
  columnNames: string[];
  checksum: string;
  externalSource?: string;
  uploadedBy: string;
  uploadedAt: string;
  profile?: DatasetProfile;
}

export interface DatasetUploadResult {
  ok: boolean;
  snapshotVersion: number;
  storageUri: string;
  format: string;
  rows: number;
  columns: string[];
  checksum: string;
  schemaEvolution?: { addedColumns: string[]; removedColumns: string[] } | null;
  profile?: DatasetProfile;
}

export interface RateLimits {
  rpm: number;
  burst: number;
  concurrent: number;
}

/**
 * Measured serving traffic over a window, from the prediction traces the serve
 * route writes. `requests: 0` means nothing has called this endpoint — it is
 * not a placeholder for an unknown value. `traceCapped` warns that the count is
 * a floor: only the most recent 50 requests per model are retained.
 */
export interface EndpointTraffic {
  windowMinutes: number;
  requests: number;
  requestsPerMinute: number;
  p95LatencyMs: number | null;
  lastRequestAt: string | null;
  traceCapped: boolean;
}

export interface Endpoint {
  modelId: string;
  title: string;
  url: string;
  status: string;
  authMethod: "OAuth 2.0 (Client Credentials)";
  tokenEndpoint: string;
  clientId: string;
  clientSecretMasked: string;
  rateLimits: RateLimits | null;
  traffic: EndpointTraffic;
}

export interface AlertPolicy {
  id: string;
  name: string;
  label: string;
  severity: "low" | "medium" | "high";
  operator: string;
  unit: string;
  window: string;
  action: string;
  enabled: boolean;
  appliesTo: string[];
  defaultThreshold: number;
  channelId: string | null;
  /**
   * Whether the evaluation engine can actually compute a value for this rule.
   * Only the eight built-in signals are evaluable; a rule outside that set can
   * be toggled on but would never fire, and the UI has to say so.
   */
  evaluable: boolean;
  /** Per-model threshold overrides that are currently set. */
  overrides: { modelId: string; threshold: number }[];
  /** The built-in signal this rule watches (a built-in rule's own id). */
  signal: string;
  /** False for a rule a user added on top of a built-in signal. */
  builtIn: boolean;
  /** Set when the rule only watches one model. */
  modelId: string | null;
  createdAt: string;
}

export interface ActiveAlert {
  modelId: string;
  policyId: string;
  policyName: string;
  label: string;
  severity: "low" | "medium" | "high";
  condition: string;
  currentValue: string;
  windowsExceeded: number;
  windowsToFire: number;
  status: "FIRED" | "BUILDING";
  ackState: "none" | "acknowledged" | "resolved";
  /** When the current breach began. */
  raisedAt: string;
}

// Matches the backend enum. QLoRA is LoRA on a 4-bit-quantized base, so it
// carries `loraParams` exactly as "lora" does.
export type FineTuneMethod = "lora" | "qlora" | "full";

export interface LoraParams {
  rank: number;
  alpha: number;
  dropout: number;
  learningRate: string;
  epochs: number;
}

export interface FullFineTuneParams {
  learningRate: string;
  epochs: number;
  warmupSteps: number;
}

export interface TrainingRunRequest {
  /** Speech-to-text (Whisper) fine-tune settings. */
  speechConfig?: { maxSamples?: number; maxEvalSamples?: number; language?: string; batchSize?: number };
  baseModelId: string;
  // What the trained model gets registered as on completion — required.
  modelName: string;
  datasetName: string;
  // Reproducibility pin: train against an exact uploaded snapshot rather than
  // "whatever the latest is at train time". Omitted resolves to latest.
  datasetVersion?: number;
  computeProfile: string;
  experimentId?: string;
  targetColumn?: string;
  features?: string[];
  splitTrain?: number;
  splitVal?: number;
  splitTest?: number;
  // Overrides for a classical-ML / deep-learning run, validated server-side
  // against the base model's declared `supported_hp`. Omitted uses the
  // trainer's own defaults.
  hyperparams?: Record<string, string | number | boolean>;
  /**
   * Per-column preprocessing, {column: strategy}. Actually applied — the trainer
   * builds each column's transformer from this and saves the fitted result with
   * the model, so it changes what the deployed model expects. Columns omitted
   * fall back to the type-appropriate default.
   */
  preprocessing?: Record<string, string>;
  fineTuneMethod?: FineTuneMethod;
  loraParams?: LoraParams;
  fullParams?: FullFineTuneParams;
  /** Which dataset columns hold the training text (LLM fine-tuning only). */
  llmFineTuneConfig?: LlmFineTuneConfig;
  /** Required by true time-series models (ARIMA, Prophet, LSTM, …) whenever
   *  the dataset has an uploaded snapshot — which column is time, which is
   *  the value, and how to treat gaps. */
  timeSeriesConfig?: TimeSeriesConfig;
}

export interface TimeSeriesConfig {
  timestampColumn: string;
  valueColumn: string;
  entityColumn?: string;
  entityValue?: string;
  frequency?: string;
  missingPeriodPolicy?: "ffill" | "interpolate" | "zero" | "drop";
  predictionHorizon?: number;
}

export interface LlmFineTuneConfig {
  textColumn?: string;
  promptColumn?: string;
  completionColumn?: string;
  maxSeqLength?: number;
  /** Train on the dataset's documents (text extracted into passages). */
  fromDocuments?: boolean;
  maxPassages?: number;
}

export interface TrainingRun extends TrainingRunRequest {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  createdAt: string;
  metrics?: Record<string, string>;
  registrationWarning?: string;
  /** Non-blocking dataset findings from the quality gate (leakage, cardinality,
   *  imbalance). Present once the run has completed. */
  qualityGateWarnings?: string[];
  /** Why the run failed, as recorded by the worker. Only set on a failed run. */
  error?: string;
  claimedBy?: string;
  attemptCount?: number;
  maxAttempts?: number;
  cancelRequested?: boolean;
  datasetSnapshotVersion?: number;
  datasetChecksum?: string;
  reproducibility?: Record<string, unknown>;
}

export type GateRowResult = "pass" | "fail" | "not-recorded";

export interface GateRow {
  key: string;
  label: string;
  threshold: number;
  direction: "min" | "max";
  scale: "percent" | "unit";
  actual: number | null;
  result: GateRowResult;
}

export type ValidationGate =
  | { applicable: false; reason: string }
  | {
      applicable: true;
      policyId: string;
      taskKind: string;
      description: string;
      rows: GateRow[];
      generalization: {
        label: string;
        maxGap: number;
        trainValue: number | null;
        validationValue: number | null;
        gap: number | null;
        result: GateRowResult;
      } | null;
      verdict: { status: "passed" | "failed" | "incomplete"; message: string };
    };

export interface TrainingRunDetail {
  run: TrainingRun;
  /** The run id doubles as its pipeline id — /pipelines/:id resolves both. */
  pipelineId: string;
  stages: PipelineStage[];
  registeredVersion: { modelId: string; v: number; alias: string } | null;
  baseModel: { id: string; title: string; uri: string; domain: Domain; task: string } | null;
  dataset: {
    name: string;
    rows: string;
    structureType: string;
    snapshotVersion: number | null;
    snapshotChecksum: string | null;
    snapshotRows: number | null;
  } | null;
  live: {
    inFlight: boolean;
    claimedBy: string | null;
    claimedAt: string | null;
    heartbeatAt: string | null;
    attemptCount: number;
    maxAttempts: number;
    cancelRequested: boolean;
    elapsedSeconds: number | null;
    note: string;
  };
  reproducibility: {
    captured: boolean;
    datasetChecksum: string | null;
    datasetSnapshotVersion: number | null;
    features: string[] | null;
    target: string | null;
    /** Trained on generated data because the dataset had no uploaded snapshot at the time. */
    syntheticData?: boolean;
    /** The split the trainer actually used: percentages, exact counts when known,
     *  how rows were divided, and whether it was logged by the trainer or derived
     *  from its fixed rule. `requested` is the wizard's split, which trainers don't apply. */
    split: {
      train: number | null; val: number | null; test: number | null;
      counts?: { train: number; val: number; test: number } | null;
      unit?: string; method?: string | null; evaluatedOn?: string | null;
      source?: "logged" | "derived" | "requested only" | string;
      requested?: { train: number; val: number; test: number } | null;
    } | null;
    seed: number | null;
    algorithm: string;
    framework: string | null;
    libraryVersions: string | null;
    pythonVersion: string | null;
    nodeVersion: string | null;
    trainerVersion: string | null;
    gitCommit: string | null;
    containerImage: string | null;
    environment: string | null;
    weightsUri: string | null;
    packageLockSha256: string | null;
    trainingRequirementsSha256: string | null;
    hyperparameters: Record<string, unknown> | null;
  };
  validationGate: ValidationGate;
  search: {
    id: string;
    objectiveMetric: string;
    objectiveDirection: string;
    numTrials: number;
    status: string;
    trials: { trialNumber: number; hyperparams: Record<string, unknown>; metricValue: number | null; status: string; isBest: boolean }[];
    best: { trialNumber: number; hyperparams: Record<string, unknown>; metricValue: number | null; algorithm: string };
  } | null;
  experiment: { id: string; name: string; objectiveMetric: string } | null;
}

export interface Experiment {
  id: string;
  name: string;
  objectiveMetric: string;
  objectiveDirection: "maximize" | "minimize";
  owner: string;
  team?: string;
  tags?: string[];
  description?: string;
  createdAt: string;
}

export interface ExperimentDetail extends Experiment {
  runs: { id: string; modelName: string; baseModelId: string; status: string; metrics?: Record<string, string>; createdAt: string }[];
  hpoSearches: HpoSearchDetail[];
  bestOverall: { source: string; label: string; value: number } | null;
}

export interface HpoTrial {
  id: string;
  trialNumber: number;
  hyperparams: Record<string, unknown>;
  metricValue: number | null;
  dataSource: "measured" | "simulated";
  status: "completed" | "failed";
  error?: string;
  isBest: boolean;
}

export interface HpoSearch {
  id: string;
  experimentId?: string;
  baseModelId: string;
  datasetName: string;
  searchSpace: Record<string, unknown[]>;
  numTrials: number;
  objectiveMetric: string;
  objectiveDirection: "maximize" | "minimize";
  status: "running" | "completed" | "failed";
  bestTrialId?: string;
  createdAt: string;
}

export interface HpoSearchDetail extends HpoSearch {
  trials: HpoTrial[];
}

export interface HpoSearchRequest {
  experimentId?: string;
  baseModelId: string;
  datasetName: string;
  datasetVersion?: number;
  targetColumn?: string;
  timeSeriesConfig?: TimeSeriesConfig & { timezone?: string };
  searchSpace: Record<string, (string | number | number[])[]>;
  numTrials: number;
  objectiveMetric: "accuracy" | "f1" | "mse" | "r2" | "mae" | "rmse" | "mape" | "reconstructionMse" | "anomalyRate" | "silhouetteScore" | "flaggedRate" | "detectionRate" | "trustworthiness" | "evalLoss" | "perplexity";
  objectiveDirection: "maximize" | "minimize";
}

export interface TargetCandidate {
  column: string;
  kind?: "numeric" | "categorical";
  cardinality?: number | null;
  distinctPct?: number | null;
  missingPct?: number | null;
  /** null = usable. "warning" = usable but the run will be noisy. "blocker" =
   *  the train-time quality gate will refuse this target. */
  severity: "blocker" | "warning" | null;
  reason: string | null;
}

export interface TargetCandidates {
  datasetName: string;
  version: number;
  rowCount: number;
  task?: string | null;
  /** False when the snapshot has no profile, or no task was supplied — the
   *  columns come back ungraded rather than graded against a guess. */
  graded: boolean;
  reason?: string;
  columns: TargetCandidate[];
}

export interface ApiError {
  error: string;
  details?: unknown;
}

export interface PipelineStage {
  order: number;
  title: string;
  state: "pending" | "running" | "ok" | "failed";
  sub: string;
}

export interface PipelineLog {
  t: string;
  /**
   * Epoch milliseconds for this line. Present on training-run pipelines, where
   * the whole run can finish inside one second — `t` alone (HH:MM:SS) collapses
   * every line to the same value. Absent on stored pipeline logs, whose `t` is
   * already a formatted string.
   */
  atMs?: number | null;
  src: string;
  level: string;
  msg: string;
  /** Structured details a trainer attached (features, params, per-step loss…). */
  data?: Record<string, unknown>;
}

export interface Pipeline {
  id: string;
  title: string;
  status: "queued" | "running" | "succeeded" | "failed";
  computeProfile: string;
  meta: string;
  startedAt: string;
  definitionId?: string | null;
  definitionVersion?: number | null;
  /** "training-run" rows have a full run detail page; "definition" rows are an
   *  execution of a stored pipeline definition and open the run page instead. */
  kind?: "training-run" | "definition";
  /** The stage in progress, else the furthest one that finished — the list's
   *  "Current stage" column, derived server-side from the same stage
   *  projection the DAG renders. */
  currentStage?: string;
}

export interface PipelineDetail extends Pipeline {
  /** Training run this pipeline produced — a training run's own id, or the one a
   *  definition's training stage created. Null until training has run. */
  trainingRunId?: string | null;
  modelName?: string | null;
  stages: PipelineStage[];
  logs: PipelineLog[];
}

export interface NotificationChannel {
  id: string;
  name: string;
  type: "slack" | "teams";
  enabled: boolean;
  webhookMasked: string;
  channelDest: string;
  /** Only alerts at or above this severity are posted. */
  minSeverity: "low" | "medium" | "high";
  /** Set when the channel only receives one model's alerts. */
  modelId: string | null;
}

export interface Deployment {
  modelId: string;
  task: string;
  domain: Domain;
  currentVersion: number;
  previousVersion: number | null;
  healthStatus: "healthy" | "degraded";
  runtime: string;
  replicas: string;
  canRollback: boolean;
}

export interface PendingPromotion {
  modelId: string;
  candidateVersion: number;
  note: string;
  task: string;
  domain: Domain;
  /** Needed for the ML / DL / LLM badge — deep learning is a framework fact. */
  framework: string;
  currentChampionVersion: number | null;
}

export interface ModelMonitoring {
  modelId: string;
  modelType: "classification" | "regression" | "generation";
  healthScore: number;
  // Loosely typed rather than one rigid shape: classification and LLM
  // (generation) models populate these with genuinely different keys —
  // the frontend renders them generically (Object.entries) rather than
  // assuming a fixed shape, the same way Infrastructure already did.
  overview: Record<string, unknown>;
  performance: {
    current: Record<string, number>;
    baseline: Record<string, number>;
    confusionMatrix?: { labels: string[]; matrix: number[][] };
  };
  drift: any;
  predictions: {
    traces: {
      input: string;
      prediction: string;
      confidence: number;
      groundTruth: string;
      correct: boolean | null;
      latencyMs: number;
      version: number;
    }[];
  };
  // Either the full attribution shape, or an honest "not available yet"
  // placeholder (available: false) for a version with no computed
  // explainability data — e.g. one that's never served production traffic.
  explainability:
    | { globalImportance: { feature: string; importance: number }[]; attributionDrift: { feature: string; baselineRank: number; currentRank: number; flagged: boolean }[]; available?: true }
    | { available: false; reason: string }
    | null;
  infrastructure: Record<string, unknown>;
  // LLM-only — null for classification/regression models.
  hallucination: Record<string, unknown> | null;
  // Deep-learning-from-scratch training metrics — null for classical ML
  // (xgboost etc, which has no epochs/loss curve) and for LLMs (which use
  // fineTuning instead, since those are fine-tuned from a pretrained base
  // rather than trained from scratch).
  trainingMetrics: DeepLearningTrainingMetrics | null;
  // Production input-quality signals for a deployed deep learning model —
  // distinct from trainingMetrics (about the training run itself).
  dlInputQuality: Record<string, unknown> | null;
  safety: Record<string, unknown> | null;
  cost: Record<string, unknown> | null;
  fineTuning: Record<string, unknown> | null;
}

export interface DeepLearningTrainingMetrics {
  // "measured" = a real scikit-learn training run actually happened and
  // these are its real numbers. "derived" = real training wasn't
  // available in this environment (e.g. no Python/scikit-learn), and
  // these are estimated from the run's final reported metric instead —
  // never silently presented as if they were measured.
  dataSource?: "measured" | "derived" | "example";
  measuredBy?: string;
  dataNote?: string;
  derivedReason?: string;
  architecture: string;
  progress: {
    epoch: number; totalEpochs: number; step: number; totalSteps: number; progressPct: number;
    samplesProcessed: string; totalSamples: string; batchesProcessed: string; elapsed: string; eta: string;
  };
  loss: {
    type: string; training: number | null; validation: number | null | undefined; bestValidation: number; bestEpoch: number;
    trainCurve: number[]; valCurve: number[];
  };
  generalization: {
    trainF1: number; validationF1: number; gap: number; status: string;
    overfittingStarted: string; bestEpoch: number; epochsWithoutImprovement: number;
  };
  optimization: {
    optimizer: string; learningRate: string; initialLearningRate: string; scheduler: string;
    gradientNorm: number | string; meanGradient: string; maxGradient: number | string;
    nanGradients: number; infGradients: number; explodingGradientEvents: number; vanishingGradientWarning: string;
    lrCurve: number[]; gradCurve: number[];
  };
  parameters: { total: string; trainable: string; frozen: string; weightNorm: number; weightMean: string | number; weightStdDev: string | number };
  throughput: { batchSize: number | string; samplesPerSec: string; batchesPerSec: string };
  compute: { gpuUtilization: string; gpuMemory: string; gpuTemperature: string; cpu: string; memory: string };
  earlyStopping: { enabled: string; monitor: string; patience: string; minDelta: string; bestScore: number; bestEpoch: number; epochsSinceImprovement: string };
  checkpoints: { epoch: number; valLoss: number; f1: number; status: string }[];
  health: { overall: number; lossConvergence: number; generalization: number; gradientHealth: number; learningRate: number; computeHealth: number; dataQuality: number; status: string };
}

export interface DatasetComment {
  id: number;
  author: string;
  body: string;
  createdAt: string;
}

export interface DatasetAuditEntry {
  id: number;
  actor: string;
  action: string;
  type: string;
  details: string[];
  createdAt: string;
}

export type ShareLevel = "read" | "download" | "upload" | "delete";
export interface DatasetAccessGrant {
  id: number;
  principal: string;
  levels: ShareLevel[];
}

export interface ComputePool {
  id: string;
  label: string;
  kind: "cpu" | "gpu";
  capacity: number;
  inUse: number;
  queuedJobs: number;
  utilizationPct: number;
}

export interface AlertHistoryEntry {
  id: number;
  modelId: string;
  policyName: string;
  label: string;
  finalValue: string;
  windowsAtResolution: number;
  resolvedBy: string;
  resolvedAt: string;
}

// ---- Real data from the Unified Catalog Service (UCS) ----
// Every section degrades independently — UCS unreachable, or this model
// never having been registered there, shows "unavailable" for that
// section specifically rather than failing the whole response.
export type UcsSection<T> = { status: "available"; data: T } | { status: "unavailable"; reason: string };

export interface LineageNode { fqn: string; job: string }
export interface LineageGraph {
  object: string;
  status: "available" | "not_found";
  upstream: LineageNode[];
  downstream: LineageNode[];
}
export interface LineageRun { run: string; job: string; state: string; ts: string | null }
export interface LineageImpact {
  object: string;
  financial_flagged: boolean;
  downstream_count: number;
  financial_downstream: number;
  downstream: (LineageNode & { financial_flagged: boolean })[];
}

export interface ModelGovernance {
  fqn: string;
  lineage: UcsSection<LineageGraph>;
  runs: UcsSection<{ runs: LineageRun[] }>;
  impact: UcsSection<LineageImpact>;
  // The real UCS object profile — genuinely open-ended (engine-specific
  // sections, tags, classifications, entitlements) so this stays loosely
  // typed rather than pretending to a fixed shape UCS itself doesn't commit to.
  profile: UcsSection<Record<string, unknown>>;
}

/**
 * Response from the in-app test-prediction routes.
 *
 * `dataSource` is the field that matters: "measured" means real weights were
 * loaded from storage and a genuine .predict() ran; "stand-in" means the
 * development-only deterministic computation answered instead, because no real
 * artifact exists for this model yet. The UI shows which one it got — the two
 * are not interchangeable and must never be rendered identically.
 */
export interface TestPrediction {
  modelId?: string;
  baseModelId?: string;
  version?: number;
  prediction: unknown;
  latencyMs: number;
  dataSource: "measured" | "stand-in";
  servedBy: string;
  servingMode: "development" | "production";
  /** false when a version was tested before deployment. */
  deployed?: boolean;
}

/** One tunable hyperparameter of a trainer, as the algorithm catalog declares it. */
export interface AlgorithmField {
  name: string;
  label: string;
  type: "number" | "text" | "select" | "yesno";
  default: string | number;
  /** Documented range, e.g. "0.01 – 0.3". Server-side validation parses it. */
  range?: string;
  required?: boolean;
  tooltip: string;
  /** Present only on family specs: the variants this field applies to. */
  showIf?: string[];
  options?: string[];
}

/**
 * The hyperparameter surface of one trainer. Several base-library entries can
 * share one spec — ridge / lasso / elasticnet are three catalog entries over
 * the one `linear_family` spec, told apart by `algorithm_variant`.
 */
export interface AlgorithmSpec {
  algorithmKey: string;
  label: string;
  appliesToText: string | null;
  hasAppliesTo: boolean;
  defaultAppliesTo: string | null;
  appliesToTooltip: string | null;
  appliesToOptions: { value: string; label: string }[] | null;
  fields: AlgorithmField[];
}

export interface PipelineDefinitionStage {
  title: string;
  type: "dataset-validation" | "preprocessing" | "training" | "hpo" | "evaluation" | "register" | "deploy" | "generic";
  params?: Record<string, unknown>;
}

/**
 * An immutable, versioned pipeline definition — what a pipeline *is*, separate
 * from any one execution of it. Saving under an existing name inserts a new
 * version rather than editing the old one.
 */
export interface PipelineDefinition {
  id: string;
  name: string;
  version: number;
  stages: PipelineDefinitionStage[];
  computeProfile: string;
  createdBy: string;
  createdAt: string;
}

/**
 * The worker fleet, derived entirely from queue state. An idle worker holds no
 * claimed row and so cannot appear — `note` carries that caveat from the server
 * rather than leaving an empty list to be misread.
 */
export interface WorkerFleet {
  measuredFrom: string;
  note: string;
  staleAfterSeconds: number;
  workers: { workerId: string; inFlightRuns: number; runIds: string[]; lastHeartbeat: string | null; secondsSinceHeartbeat: number | null; stale: boolean }[];
  queuedRuns: number;
  hpoWorkers: { workerId: string; inFlightTrials: number; trialIds: string[]; lastHeartbeat: string | null; secondsSinceHeartbeat: number | null; stale: boolean }[];
  queuedHpoTrials: number;
  /** Every running worker process, idle ones included — from the heartbeat each
   *  worker loop writes per poll cycle. Absent on a server without the registry. */
  fleet?: FleetWorker[];
  liveWithinSeconds?: number;
}

export interface FleetWorker {
  workerId: string;
  hostname: string;
  pid: number;
  /** cpu / gpu from the compute profiles it claims; "any" when unrestricted. */
  pool: "cpu" | "gpu" | "any";
  roles: ("training" | "hpo")[];
  capacity: number;
  inFlight: number;
  secondsSinceHeartbeat: number;
  live: boolean;
  current: { kind: "training-run" | "hpo-trial"; id: string; label: string }[];
}

/**
 * One version as a promotion decision sees it. `metrics` is null when this
 * version has no linked training run — the figures were never recorded, which
 * is a different statement from them being zero.
 */
export interface PromotionVersion {
  version: number;
  alias: string;
  note: string;
  trainedAt: string | null;
  baseModelId: string | null;
  computeProfile: string | null;
  hyperparams: Record<string, unknown> | null;
  datasetName: string | null;
  datasetSnapshotVersion: number | null;
  datasetChecksum: string | null;
  weightsUri: string | null;
  artifactFormat: string | null;
  actualFramework: string | null;
  metrics: Record<string, number | string> | null;
  hasTrainingRun: boolean;
}

/** A computed predicate over real data. "unknown" means the underlying fact
 *  isn't recorded — not the same as a failed check. */
export interface PromotionCheck {
  label: string;
  status: "pass" | "fail" | "unknown";
  detail: string;
}

export interface PromotionDetail {
  modelId: string;
  task: string;
  domain: Domain;
  objectiveMetric: string | null;
  comparableMetrics: string[];
  candidate: PromotionVersion;
  champion: PromotionVersion | null;
  checklist: PromotionCheck[];
}

export interface ComputePoolsResponse {
  measuredFrom: string;
  pools: ComputePool[];
}

/**
 * What the platform can and cannot see about the cluster underneath it. Every
 * section is reported with `available` and what it would need — deliberately
 * not zeros, which would read as "measured and empty".
 */
export interface ClusterTelemetry {
  prometheusConfigured: boolean;
  prometheusUrl: string | null;
  reason: string;
  sections: { name: string; available: boolean; requires: string }[];
}

/** One card on the Model Overview landing grid. `healthScore` is null — not
 *  zero — for a model that has never served measured traffic: zero is a
 *  verdict, "never measured" is not. */
export type ModelOverviewCard = {
  modelId: string;
  task: string;
  domain: string;
  kind: string | null;
  framework: string | null;
  updated: string | null;
  category: string;
  version: number | null;
  alias: string | null;
  deployed: boolean;
  runtime: string | null;
  deploymentHealth: string | null;
  healthScore: number | null;
  status: string;
  coveragePct: number | null;
};

/** Global feature attribution read out of the fitted model itself
 *  (feature_importances_ for trees, |coef_| for linear models). Absent for
 *  estimators that expose neither — reported, not faked. */
export type ModelExplainability = {
  available: boolean;
  reason?: string;
  method?: string;
  grouping?: string;
  modelClass?: string;
  featureCount?: number;
  version?: number;
  computedAt?: string;
  note?: string;
  features?: Array<{ feature: string; importance: number; sharePct: number }>;
};

/** The columns a deployed model's artifact was actually fitted on, plus a
 *  ready-to-send example built from that model's own training data. */
export type ModelInputSchema = {
  available: boolean;
  reason?: string;
  modelId: string;
  version?: number;
  columns?: string[];
  example?: Record<string, unknown>;
  exampleSource?: string;
};

/** Real per-day series computed from stored prediction traces, ground-truth
 *  feedback and recorded health snapshots. A day with no traffic has no point. */
export type MetricPoint = { t: string; value: number };
export type ModelTimeseries = {
  modelId: string;
  buckets: string;
  performance: { accuracy: MetricPoint[]; precision: MetricPoint[]; recall: MetricPoint[]; f1: MetricPoint[]; groundTruthCount: number };
  serving: { volume: MetricPoint[]; p95Latency: MetricPoint[]; avgConfidence: MetricPoint[] };
  health: { score: MetricPoint[]; maxPsi: MetricPoint[] };
  note: string;
};
