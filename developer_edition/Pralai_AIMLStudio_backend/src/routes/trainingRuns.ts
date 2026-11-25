import { relevantLibraries } from "../services/reproducibility";
import { Router, Request, Response } from "express";
import { z } from "zod";
import { randomUUID } from "crypto";
import { pool } from "../db/connection";
import { hasPermission } from "@aadhyaanalyticsinc/gateway-auth";
import { PERM, requireAnyPermission, requirePermission, requireUcsModelWrite, requireUcsTrainingWrite } from "../middleware/permissions";
import { registerTrainedModel } from "../services/modelRegistration";
import { killRunProcesses } from "../services/runLog";
import { TIME_SERIES_ONLY_IDS } from "../services/classicalModelMapping";
import { evaluateValidationGate } from "../services/validationGate";
import { runStages } from "./pipelines";

/**
 * The split a run actually trained with. `actualSplit` (in reproducibility) comes from
 * the trainer itself — exact row counts and how rows were divided — or, for runs that
 * predate that, is derived from the trainer's fixed rule (source "derived"). The wizard's
 * requested split is returned beside it: no trainer applies it today, so showing the
 * request as if it were the split would be wrong.
 */
function actualSplitDto(a: { train?: number | null; validation?: number | null; test?: number | null; unit?: string; method?: string; evaluatedOn?: string; source?: string; trainPct?: number; valPct?: number; testPct?: number } | null | undefined, r: any) {
  const requested = r.split_train != null ? { train: Number(r.split_train), val: Number(r.split_val ?? 0), test: Number(r.split_test ?? 0) } : null;
  if (!a) return requested ? { train: requested.train, val: requested.val, test: requested.test, requested, method: null, source: "requested only" } : null;
  const counts = a.train != null ? { train: a.train ?? 0, val: a.validation ?? 0, test: a.test ?? 0 } : null;
  const total = counts ? counts.train + counts.val + counts.test : 0;
  const pct = (n: number) => Math.round((n / total) * 1000) / 10;
  return {
    train: counts && total ? pct(counts.train) : a.trainPct ?? null,
    val: counts && total ? pct(counts.val) : a.valPct ?? null,
    test: counts && total ? pct(counts.test) : a.testPct ?? null,
    counts, unit: a.unit ?? "rows", method: a.method ?? null, evaluatedOn: a.evaluatedOn ?? null,
    source: a.source ?? "logged", requested,
  };
}

export const trainingRunsRouter = Router();

// Shared by both the manual POST .../complete route (kept for explicit/
// backward-compat use) and the real background worker
// (services/trainingWorker.ts) — one real implementation of "what
// completing a run means," not two to keep in sync. Both callers reach
// this only after already atomically claiming the run (status flipped
// queued/running -> running by THIS caller specifically), so there's no
// double-completion race between a manual call and the worker.
export async function completeTrainingRun(run: any, actor: string): Promise<{ metrics: Record<string, unknown>; registrationWarning?: string; qualityGateWarnings?: string[] }> {
  // No simulated summary metrics: production baselines and run results are populated only from the real trainer.
  const submittedMetrics: Record<string, unknown> = {};

  let registrationWarning: string | undefined;
  let measuredMetrics: Record<string, unknown> | null = null;
  let qualityGateWarnings: string[] | undefined;
  try {
    const registration = await registerTrainedModel(pool, { ...run, metrics: submittedMetrics }, actor);
    measuredMetrics = registration.measuredMetrics;
    qualityGateWarnings = registration.qualityGateWarnings.length ? registration.qualityGateWarnings : undefined;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    // Five real, distinct reasons training never actually ran against
    // real data as intended — a quality-gate blocker, a pin that
    // couldn't be resolved, DATASET_UNAVAILABLE (a real snapshot WAS
    // resolved but couldn't actually be retrieved), TRAINING_EXECUTION_FAILED
    // (the real trainer subprocess itself errored — a bug, a bad
    // hyperparameter, an OOM, a timeout), ARTIFACT_PERSISTENCE_FAILED
    // (production requires a durable model artifact and the upload/verify
    // step failed), or the base model named
    // simply not existing at all — there's no real base model row to
    // even determine a real domain/framework/task from, so no real
    // trainer could ever have been dispatched; this isn't "training
    // succeeded but cataloging it failed," it's "there was nothing real
    // to train." All five used to (or, for the last one, still would
    // have) silently fall back to synthetic/fabricated metrics and
    // report "succeeded," which is worse than a loud failure: a wrong
    // result presented as a right one. Re-thrown so the caller marks
    // the run 'failed' with the real reason, not 'succeeded' with a
    // misleading "registration failed" message — that's what this catch
    // was originally built for, and remains the fallback for genuinely
    // separate issues like a UCS outage after real training already
    // succeeded.
    if (message.includes("failed real quality checks") || message.includes("DATASET_UNAVAILABLE") || message.includes("no real snapshot at the requested version") || message.includes("TRAINING_EXECUTION_FAILED") || message.includes("ARTIFACT_PERSISTENCE_FAILED") || /base model '.*' not found/.test(message)) {
      // A cancelled run's trainer was killed on purpose: keep "cancelled" as the reason.
      await pool.query(
        "UPDATE training_runs SET status = 'failed', error = CASE WHEN cancel_requested THEN 'CANCELLED: cancelled while training' ELSE $1 END WHERE id = $2",
        [message, run.id]);
      throw err;
    }
    registrationWarning = `Training succeeded, but registering '${run.model_name}' in the catalog failed: ${message}. It won't appear in Model Catalog or Model Overview until this is retried.`;
    // eslint-disable-next-line no-console
    console.error(registrationWarning);
  }

  const metrics = measuredMetrics ?? {};
  // Persisted, not just returned: the background worker is the normal
  // completion path and it drops the return value, so warnings computed here
  // were previously unreachable from the API or the UI.
  await pool.query(
    "UPDATE training_runs SET status = 'succeeded', metrics = $1, quality_gate_warnings = $2::jsonb WHERE id = $3 AND NOT cancel_requested",
    [JSON.stringify(metrics), qualityGateWarnings ? JSON.stringify(qualityGateWarnings) : null, run.id]
  );
  return { metrics, registrationWarning, qualityGateWarnings };
}

const loraParamsSchema = z.object({
  rank: z.number().int().positive(),
  alpha: z.number().int().positive(),
  dropout: z.number().min(0).max(1),
  learningRate: z.string(),
  epochs: z.number().int().positive(),
});

const fullParamsSchema = z.object({
  learningRate: z.string(),
  epochs: z.number().int().positive(),
  warmupSteps: z.number().int().nonnegative(),
});

export const trainingRunRequestSchema = z.object({
  baseModelId: z.string(),
  // What this trained model gets registered as on completion — required,
  // since "train an unnamed model" isn't a real registration in any real
  // platform (Databricks, SageMaker, MLflow all require naming the model
  // being registered, since one base model can produce many of them).
  modelName: z.string().min(1, "Give the trained model a name — it's what gets registered in the catalog on completion."),
  datasetName: z.string(),
  // Explicit reproducibility pin — "train against fraud-transactions-2026
  // v12," not just "whatever v currently is." Omitted means the existing
  // default: resolve the latest real snapshot at train time. Validated
  // against real dataset_snapshots below, not just accepted as a number
  // — a request for a version that was never uploaded is a real 404,
  // not a silent fall-through to latest or to synthetic data.
  datasetVersion: z.number().int().positive().optional(),
  computeProfile: z.string(),
  experimentId: z.string().uuid().optional(),
  targetColumn: z.string().optional(),
  features: z.array(z.string()).optional(),
  splitTrain: z.number().optional(),
  splitVal: z.number().optional(),
  splitTest: z.number().optional(),
  // Hyperparameter overrides for a classical-ML / deep-learning run. The
  // trainers have always read `hyperparams` (see training/*.py) and HPO has
  // always written it, but nothing let the Train wizard set it — a run
  // launched from the UI could only ever use the trainer's built-in defaults,
  // even for a base model whose tunable surface the catalog knows exactly.
  // Validated against that surface below, so a typo'd or out-of-range value
  // fails here rather than inside a Python worker minutes later.
  hyperparams: z.record(z.union([z.string(), z.number(), z.boolean()])).optional(),
  // Per-column preprocessing overrides, {column: strategy}. The trainer fits
  // and saves the resulting transformer alongside the model, so this genuinely
  // changes what the deployed model expects — it is not a display preference.
  // Columns left out fall back to the type-appropriate default.
  preprocessing: z.record(z.enum(["standard_scale", "min_max_scale", "one_hot", "ordinal", "passthrough", "none"])).optional(),
  fineTuneMethod: z.enum(["lora", "qlora", "full"]).optional(),
  loraParams: loraParamsSchema.optional(),
  fullParams: fullParamsSchema.optional(),
  // The real dataset contract LLM fine-tuning needs — either a single
  // real column already containing complete training text, or a real
  // prompt/completion column pair joined with the real tokenizer's own
  // EOS token. Required whenever fineTuneMethod is set and a real
  // dataset snapshot exists — there's no honest synthetic-text fallback
  // the way classical/DL training has a synthetic-tabular one.
  // Speech-to-text (Whisper) fine-tuning: caps keep a local GPU run to minutes, not hours.
  speechConfig: z.object({
    maxSamples: z.number().int().min(10).max(20000).optional(),
    maxEvalSamples: z.number().int().min(2).max(500).optional(),
    language: z.string().trim().min(2).max(12).optional(),
    batchSize: z.number().int().min(1).max(32).optional(),
  }).optional(),
  llmFineTuneConfig: z.object({
    textColumn: z.string().optional(),
    promptColumn: z.string().optional(),
    completionColumn: z.string().optional(),
    maxSeqLength: z.number().int().positive().max(8192).optional(),
    // Fine-tune on documents (PDF/Markdown/text/HTML/Word inside an unstructured dataset):
    // their text is extracted and split into passages that fill `text`.
    fromDocuments: z.boolean().optional(),
    maxPassages: z.number().int().min(10).max(500000).optional(),
  }).refine((c) => !!c.textColumn || !!(c.promptColumn && c.completionColumn), {
    message: "llmFineTuneConfig needs either textColumn, or both promptColumn and completionColumn",
  }).optional(),
  // The real time-series dataset contract — required when training one
  // of the 6 true time-series base models against a real dataset. A
  // generic targetColumn doesn't establish enough of what a real
  // forecasting dataset needs (which column is time, how gaps are
  // handled, which real series out of a multi-entity dataset).
  timeSeriesConfig: z.object({
    timestampColumn: z.string().min(1),
    valueColumn: z.string().min(1),
    entityColumn: z.string().optional(),
    entityValue: z.string().optional(),
    frequency: z.string().optional(),
    timezone: z.string().optional(),
    missingPeriodPolicy: z.enum(["ffill", "interpolate", "zero", "drop"]).default("ffill"),
    globalModel: z.boolean().optional(),
    predictionHorizon: z.number().int().positive().max(365).optional(),
    knownRealColumns: z.array(z.string()).optional(),
    unknownRealColumns: z.array(z.string()).optional(),
    knownCategoricalColumns: z.array(z.string()).optional(),
    staticCategoricalColumns: z.array(z.string()).optional(),
  }).optional(),
});

function toRunDto(row: any) {
  return {
    id: row.id,
    baseModelId: row.base_model_id,
    modelName: row.model_name,
    experimentId: row.experiment_id ?? undefined,
    datasetName: row.dataset_name,
    requestedDatasetVersion: row.requested_dataset_version ?? undefined,
    timeSeriesConfig: row.time_series_config ?? undefined,
    llmFineTuneConfig: row.llm_fine_tune_config ?? undefined,
    computeProfile: row.compute_profile,
    status: row.status,
    createdAt: row.created_at,
    targetColumn: row.target_column ?? undefined,
    features: row.features ?? undefined,
    splitTrain: row.split_train ?? undefined,
    splitVal: row.split_val ?? undefined,
    splitTest: row.split_test ?? undefined,
    fineTuneMethod: row.fine_tune_method ?? undefined,
    loraParams: row.lora_params ?? undefined,
    fullParams: row.full_params ?? undefined,
    hyperparams: row.hyperparams ?? undefined,
    preprocessing: row.preprocessing ?? undefined,
    metrics: row.metrics ?? undefined,
    qualityGateWarnings: row.quality_gate_warnings ?? undefined,
    claimedBy: row.claimed_by ?? undefined,
    attemptCount: row.attempt_count,
    maxAttempts: row.max_attempts,
    cancelRequested: row.cancel_requested,
    error: row.error ?? undefined,
    datasetSnapshotVersion: row.dataset_snapshot_version ?? undefined,
    datasetChecksum: row.dataset_checksum ?? undefined,
    reproducibility: row.reproducibility ?? undefined,
  };
}

trainingRunsRouter.get("/training-runs", requireAnyPermission(PERM.pipelines, PERM.train, PERM.dashboard, PERM.clusters, PERM.experiments, PERM.modelCatalog), async (_req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM training_runs ORDER BY created_at DESC");
  res.json(result.rows.map(toRunDto));
});

trainingRunsRouter.get("/training-runs/:id", requireAnyPermission(PERM.pipelines, PERM.train), async (req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM training_runs WHERE id = $1", [req.params.id]);
  if (result.rows.length === 0) return res.status(404).json({ error: "run not found" });
  res.json(toRunDto(result.rows[0]));
});

/**
 * GET /api/training-runs/:id/detail — everything the Training run detail page
 * shows, in one round trip.
 *
 * Composed rather than invented. Each block below names where it comes from,
 * and a block with nothing real behind it is returned as null with a reason
 * instead of a plausible-looking default:
 *
 *   stages          reuse of the Pipelines projection, so the DAG on this page
 *                   and the DAG on the Pipelines page cannot disagree
 *   repro           training_runs.reproducibility, captured at registration
 *   gate            the stored acceptance policy, evaluated against this run's
 *                   own measured metrics (see services/validationGate.ts)
 *   search          the HPO search this run came from, if any, with its real
 *                   trials — null for a run launched directly
 *   experiment      the experiment this run belongs to, if any
 *   live            worker claim/heartbeat facts while a run is in flight
 *
 * Deliberately NOT here: per-step training progress and CPU/memory telemetry.
 * A run executes as a single trainer subprocess that reports once, at the end —
 * there is no per-tree or per-epoch progress channel to read, so a progress bar
 * here could only be a timer pretending to be a measurement. `live.note` says
 * this in the response rather than leaving the caller to wonder.
 */
trainingRunsRouter.get("/training-runs/:id/detail", requireAnyPermission(PERM.pipelines, PERM.train, PERM.modelOverview), async (req: Request, res: Response) => {
  const result = await pool.query(
    `SELECT t.*, EXISTS (SELECT 1 FROM model_versions v WHERE v.training_run_id = t.id) AS has_version
       FROM training_runs t WHERE t.id = $1`,
    [req.params.id]
  );
  if (result.rows.length === 0) return res.status(404).json({ error: "run not found" });
  const r = result.rows[0];

  const base = await pool.query("SELECT id, title, uri, domain, framework, task FROM base_models WHERE id = $1", [r.base_model_id]);
  const baseModel = base.rows[0] ?? null;

  const dataset = await pool.query(
    `SELECT d.name, d.rows, d.structure_type,
            s.version AS snapshot_version, s.checksum AS snapshot_checksum, s.row_count AS snapshot_rows
       FROM datasets d
       LEFT JOIN dataset_snapshots s
              ON s.dataset_name = d.name AND s.version = $2
      WHERE d.name = $1`,
    [r.dataset_name, r.dataset_snapshot_version]
  );
  const ds = dataset.rows[0] ?? null;

  const version = await pool.query(
    "SELECT model_id, v, alias FROM model_versions WHERE training_run_id = $1 ORDER BY v DESC LIMIT 1",
    [r.id]
  );

  // An HPO search owns this run when promote-best launched it from a winning
  // trial. Matched on the recorded hyperparameters + model, which is the only
  // link that exists — there is no search_id column on training_runs.
  let search: Record<string, unknown> | null = null;
  const searchRow = await pool.query(
    `SELECT * FROM hpo_searches
      WHERE base_model_id = $1 AND dataset_name = $2 AND created_at <= $3
      ORDER BY created_at DESC LIMIT 1`,
    [r.base_model_id, r.dataset_name, r.created_at]
  );
  if (searchRow.rows.length && r.hyperparams) {
    const sr = searchRow.rows[0];
    const trials = await pool.query("SELECT * FROM hpo_trials WHERE search_id = $1 ORDER BY trial_number ASC", [sr.id]);
    const best = trials.rows.find((t: any) => t.id === sr.best_trial_id) ?? null;
    // Only claim the link when the winning trial's hyperparameters are
    // genuinely the ones this run was launched with. A same-model/same-dataset
    // search that produced a different candidate is a different lineage, and
    // showing its leaderboard here would be a false attribution.
    const sameParams = best && JSON.stringify(best.hyperparams) === JSON.stringify(r.hyperparams);
    if (sameParams) {
      search = {
        id: sr.id,
        objectiveMetric: sr.objective_metric,
        objectiveDirection: sr.objective_direction,
        numTrials: sr.num_trials,
        status: sr.status,
        trials: trials.rows.map((t: any) => ({
          trialNumber: t.trial_number,
          hyperparams: t.hyperparams,
          metricValue: t.metric_value === null ? null : Number(t.metric_value),
          status: t.status,
          isBest: t.id === sr.best_trial_id,
        })),
        best: {
          trialNumber: best.trial_number,
          hyperparams: best.hyperparams,
          metricValue: best.metric_value === null ? null : Number(best.metric_value),
          algorithm: baseModel?.title ?? r.base_model_id,
        },
      };
    }
  }

  let experiment: { id: string; name: string; objectiveMetric: string } | null = null;
  if (r.experiment_id) {
    const e = await pool.query("SELECT id, name, objective_metric FROM experiments WHERE id = $1", [r.experiment_id]);
    if (e.rows.length) experiment = { id: e.rows[0].id, name: e.rows[0].name, objectiveMetric: e.rows[0].objective_metric };
  }

  const gate = await evaluateValidationGate(pool, { status: r.status, metrics: r.metrics ?? null });

  const inFlight = r.status === "queued" || r.status === "running";
  const startedMs = r.claimed_at ? new Date(r.claimed_at).getTime() : null;
  const endedMs = inFlight ? Date.now() : r.heartbeat_at ? new Date(r.heartbeat_at).getTime() : null;
  const repro = (r.reproducibility ?? {}) as Record<string, unknown>;

  // An LLM fine-tune records its columns in llm_fine_tune_config, not in the classical
  // features / target_column / split_* fields — so the page showed "not recorded" and
  // "n/a — unsupervised" for what is supervised training on a known answer column.
  // The split is the trainer's own: 85% train / 15% evaluation (llm_finetune_trainer.py).
  const llmCfg = r.llm_fine_tune_config as { promptColumn?: string; completionColumn?: string; textColumn?: string } | null;
  let llmShape: { features: string[]; target: string; split: { train: number; val: number; test: number } } | null = null;
  if (llmCfg) {
    const cols = r.dataset_snapshot_version != null
      ? (await pool.query("SELECT column_names FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2",
          [r.dataset_name, r.dataset_snapshot_version])).rows[0]?.column_names ?? []
      : [];
    llmShape = llmCfg.completionColumn
      ? { features: [...(cols.includes("system") ? ["system"] : []), llmCfg.promptColumn ?? "prompt"], target: llmCfg.completionColumn,
          split: { train: 85, val: 15, test: 0 } }
      : { features: [llmCfg.textColumn ?? "text"], target: `${llmCfg.textColumn ?? "text"} (next-token prediction)`,
          split: { train: 85, val: 15, test: 0 } };
  }

  // What the trainer actually trained on, for runs that didn't record an explicit
  // feature list / target: a forecaster learns from its time index and the value's own
  // history; a tabular run without a feature list used every column except the target.
  const syntheticData = r.dataset_snapshot_version == null && (r.metrics as any)?.realDataset === false;
  // classical_trainer.py's generated fallback: 600 rows of make_classification (14 features),
  // make_regression (12) or make_blobs (10) — so a synthetic run's inputs are known exactly.
  const syntheticFeatureCount = syntheticData ? ({ Regression: 12, Clustering: 10 } as Record<string, number>)[baseModel?.task ?? ""] ?? 14 : 0;
  const tsCfg = r.time_series_config as { timestampColumn?: string; valueColumn?: string; knownRealColumns?: string[]; unknownRealColumns?: string[] } | null;
  const snapCols: string[] = r.dataset_snapshot_version != null
    ? (await pool.query("SELECT column_names FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2",
        [r.dataset_name, r.dataset_snapshot_version])).rows[0]?.column_names ?? []
    : [];
  const derivedFeatures: string[] | null = (r.features?.length ? r.features : null)
    ?? (tsCfg?.timestampColumn
      ? [`${tsCfg.timestampColumn} (time index)`, `${tsCfg.valueColumn} (lagged history)`, ...(tsCfg.knownRealColumns ?? []), ...(tsCfg.unknownRealColumns ?? [])]
      : null)
    ?? llmShape?.features
    ?? (snapCols.length ? snapCols.filter((c) => c !== r.target_column) : null)
    ?? (syntheticFeatureCount ? [`${syntheticFeatureCount} generated features (synthetic data — no dataset file was uploaded)`] : null);
  const derivedTarget: string | null = r.target_column ?? tsCfg?.valueColumn ?? llmShape?.target ?? null;
  const llmFramework = r.fine_tune_method
    ? r.fine_tune_method === "full" ? "PyTorch · Hugging Face Transformers (full fine-tune)" : `PyTorch · Hugging Face Transformers + PEFT (${String(r.fine_tune_method).toUpperCase()})`
    : null;
  const llmHyper = r.fine_tune_method
    ? { ...(r.lora_params ?? r.full_params ?? {}), ...(r.llm_fine_tune_config?.maxSeqLength ? { maxSeqLength: r.llm_fine_tune_config.maxSeqLength } : {}) }
    : null;

  res.json({
    run: toRunDto(r),
    pipelineId: r.id,
    stages: runStages(r, r.has_version),
    registeredVersion: version.rows[0]
      ? { modelId: version.rows[0].model_id, v: version.rows[0].v, alias: version.rows[0].alias }
      : null,
    baseModel: baseModel
      ? { id: baseModel.id, title: baseModel.title, uri: baseModel.uri, domain: baseModel.domain, task: baseModel.task }
      : null,
    dataset: ds
      ? {
          name: ds.name,
          rows: ds.rows,
          structureType: ds.structure_type,
          snapshotVersion: ds.snapshot_version ?? null,
          snapshotChecksum: ds.snapshot_checksum ?? r.dataset_checksum ?? null,
          snapshotRows: ds.snapshot_rows ?? null,
        }
      : null,
    live: {
      inFlight,
      claimedBy: r.claimed_by ?? null,
      claimedAt: r.claimed_at ?? null,
      heartbeatAt: r.heartbeat_at ?? null,
      attemptCount: r.attempt_count,
      maxAttempts: r.max_attempts,
      cancelRequested: r.cancel_requested,
      elapsedSeconds: startedMs && endedMs ? Math.max(0, Math.round((endedMs - startedMs) / 1000)) : null,
      note: "Elapsed, claim and heartbeat are read from the run row the worker maintains. Step-level progress and CPU/memory are not shown because nothing measures them: the trainer runs as one subprocess and reports once, on completion.",
    },
    reproducibility: {
      captured: Object.keys(repro).length > 0,
      datasetChecksum: (repro.datasetChecksum as string) ?? r.dataset_checksum ?? null,
      datasetSnapshotVersion: r.dataset_snapshot_version ?? null,
      features: derivedFeatures,
      target: derivedTarget,
      syntheticData,
      split: actualSplitDto(repro.actualSplit as any, r),
      seed: (repro.seed as number) ?? null,
      algorithm: (repro.actualArchitecture as string) ?? baseModel?.title ?? r.base_model_id,
      framework: (repro.actualFramework as string) ?? llmFramework ?? null,
      libraryVersions: relevantLibraries((repro.libraryVersions as string) ?? null, baseModel?.domain, baseModel?.framework),
      pythonVersion: (repro.pythonVersion as string) ?? null,
      nodeVersion: (repro.nodeVersion as string) ?? null,
      trainerVersion: (repro.trainerVersion as string) ?? null,
      gitCommit: (repro.gitCommit as string) ?? null,
      containerImage: (repro.containerImage as string) ?? null,
      environment: [baseModel?.uri, r.compute_profile, repro.platform, repro.arch].filter(Boolean).join(" · ") || null,
      weightsUri: (repro.weightsUri as string) ?? null,
      packageLockSha256: (repro.packageLockSha256 as string) ?? null,
      trainingRequirementsSha256: (repro.trainingRequirementsSha256 as string) ?? null,
      // What the run actually trained with (trainer-reported, defaults included); the
      // requested overrides only for runs that predate that report.
      hyperparameters: (repro.effectiveHyperparams as Record<string, unknown>) ?? llmHyper ?? r.hyperparams ?? null,
    },
    validationGate: gate,
    search,
    experiment,
  });
});

// POST /api/training-runs — the real backend counterpart to the Train wizard's
// "Launch training run" button. Enforces the SAME domain split the wizard's UI
// does (classical-ML fields vs. LoRA/full-fine-tune fields), server-side —
// the UI hiding the wrong fields isn't a substitute for actually validating it.
// The one place a training run is created. Re-train goes through it too, so a
// re-run is validated, authorised and dataset-pinned exactly like a new one.
async function createTrainingRun(req: Request, res: Response) {
  const parsed = trainingRunRequestSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({ error: "invalid training run request", details: parsed.error.flatten() });
  }
  const body = parsed.data;

  // gpu-4x/gpu-8x need GPU quota approval: only a caller holding
  // mlstudio:gpu:train-large may launch against them — the same restriction
  // the compute-profile picker shows.
  if ((body.computeProfile === "gpu-4x" || body.computeProfile === "gpu-8x") && !hasPermission(req.principal, PERM.gpuLarge)) {
    return res.status(403).json({ error: `${body.computeProfile} requires GPU quota approval (missing permission ${PERM.gpuLarge})` });
  }

  const baseModelResult = await pool.query("SELECT * FROM base_models WHERE id = $1", [body.baseModelId]);
  if (baseModelResult.rows.length === 0) {
    return res.status(404).json({ error: `base model '${body.baseModelId}' not found` });
  }
  const baseModel = baseModelResult.rows[0];
  // Text-to-speech (Kokoro) generates speech in ML Studio but has no fine-tuning method.
  if (baseModel && baseModel.task === "Text-to-speech") {
    return res.status(422).json({
      error: `${baseModel.id} can't be fine-tuned — Kokoro has no supported training method. It does generate speech: open the model and use "Test this model".`,
    });
  }
  if (baseModel.id === "cnn-image-classifier" && !body.targetColumn) body.targetColumn = "label";

  const datasetResult = await pool.query("SELECT * FROM datasets WHERE name = $1", [body.datasetName]);
  if (datasetResult.rows.length === 0) {
    return res.status(404).json({ error: `dataset '${body.datasetName}' not found` });
  }
  const dataset = datasetResult.rows[0];

  // Real validation, not a silent fall-through — a request pinned to a
  // specific version that was never uploaded (a typo, a version that
  // predates this dataset's real upload history, or simply wrong) is a
  // real 404 now, not something that quietly trained on the latest
  // version (or synthetic data) instead while claiming to honor the pin.
  if (body.datasetVersion !== undefined) {
    const snapshotCheck = await pool.query(
      "SELECT 1 FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2",
      [body.datasetName, body.datasetVersion]
    );
    if (snapshotCheck.rows.length === 0) {
      return res.status(404).json({ error: `dataset '${body.datasetName}' has no real uploaded snapshot at version ${body.datasetVersion} — check GET /api/datasets/${body.datasetName}/snapshots for what actually exists` });
    }
  }

  // If this model name is already registered against a DIFFERENT base
  // model, refuse rather than silently letting a new run's completion
  // overwrite an unrelated model's lineage.
  const existingCatalog = await pool.query("SELECT base_ref FROM catalog_models WHERE id = $1", [body.modelName]);
  if (existingCatalog.rows.length > 0 && existingCatalog.rows[0].base_ref !== body.baseModelId) {
    return res.status(409).json({
      error: `'${body.modelName}' is already registered as a model trained from '${existingCatalog.rows[0].base_ref}' — pick a different name, or use '${existingCatalog.rows[0].base_ref}' as the base model to add a new version to it`,
    });
  }

  // Validate hyperparameter overrides against this base model's declared
  // tunable surface. An unknown key is almost always a typo that the Python
  // trainer would silently ignore, leaving you with a run that looks configured
  // but trained on defaults — so it's rejected here with the list of what this
  // model actually accepts. Bounds and enum values are enforced for the same
  // reason: failing at submit is honest, failing inside a worker ten minutes in
  // is not.
  if (body.hyperparams) {
    if (baseModel.domain === "llm") {
      return res.status(400).json({ error: "hyperparams doesn't apply to an LLM fine-tuning run — use loraParams or fullParams" });
    }
    // Two catalogs describe a hyperparameter surface and both are real:
    // `algorithm_specs` is the full per-trainer form the wizard renders (56
    // algorithms, keyed by algorithm_key), and `base_models.supported_hp` is
    // the narrower HPO-facing schema that a handful of entries carry. Validate
    // against whichever this base model actually has, preferring the richer one.
    const algoRow = baseModel.algorithm_key
      ? await pool.query("SELECT * FROM algorithm_specs WHERE algorithm_key = $1", [baseModel.algorithm_key])
      : { rows: [] as any[] };
    const algoSpec = algoRow.rows[0] ?? null;
    const supported = (baseModel.supported_hp ?? null) as Record<string, any> | null;

    if (!algoSpec && !supported) {
      return res.status(422).json({
        error: `base model '${baseModel.id}' has no cataloged hyperparameter schema, so overrides can't be validated against anything — launch without hyperparams to use the trainer's own defaults`,
      });
    }

    if (algoSpec) {
      // A family field only exists for the variants it lists — offering
      // l1_ratio on a plain ridge model would accept a value the estimator
      // has no parameter for.
      const variant = baseModel.algorithm_variant || algoSpec.default_applies_to;
      const fields: any[] = (algoSpec.fields as any[]).filter((f) => !f.showIf || (variant && f.showIf.includes(variant)));
      const byName = new Map(fields.map((f) => [f.name, f]));
      for (const [key, value] of Object.entries(body.hyperparams)) {
        const field = byName.get(key);
        if (!field) {
          return res.status(400).json({
            error: `'${key}' is not a hyperparameter of '${baseModel.id}' (${algoSpec.label}${variant ? `, ${variant}` : ""}) — accepted: ${[...byName.keys()].join(", ")}`,
          });
        }
        if (field.type === "select" && Array.isArray(field.options) && !field.options.map(String).includes(String(value))) {
          return res.status(400).json({ error: `'${key}' must be one of: ${field.options.join(", ")}` });
        }
        if (field.type === "yesno" && !["yes", "no"].includes(String(value))) {
          return res.status(400).json({ error: `'${key}' must be yes or no` });
        }
        if (field.type === "number") {
          if (typeof value !== "number" || !Number.isFinite(value)) {
            return res.status(400).json({ error: `'${key}' must be a number` });
          }
          // `range` is documentation ("0.01 – 0.3"), so parse the two ends
          // rather than treating it as free text — a value outside the range
          // the catalog publishes is a real misconfiguration.
          const bounds = String(field.range ?? "").match(/(-?[\d.]+)\s*[–-]\s*(-?[\d.]+)/);
          if (bounds) {
            const [lo, hi] = [Number(bounds[1]), Number(bounds[2])];
            if (Number.isFinite(lo) && Number.isFinite(hi) && (value < lo || value > hi)) {
              return res.status(400).json({ error: `'${key}' must be within ${field.range}` });
            }
          }
        }
      }
    } else if (supported) {
      for (const [key, value] of Object.entries(body.hyperparams)) {
        const spec = supported[key];
        if (!spec) {
          return res.status(400).json({ error: `'${key}' is not a hyperparameter of '${baseModel.id}' — accepted: ${Object.keys(supported).join(", ")}` });
        }
        if (spec.type === "enum" && Array.isArray(spec.values) && !spec.values.includes(value)) {
          return res.status(400).json({ error: `'${key}' must be one of: ${spec.values.join(", ")}` });
        }
        if ((spec.type === "int" || spec.type === "float") && typeof value === "number") {
          if (typeof spec.min === "number" && value < spec.min) {
            return res.status(400).json({ error: `'${key}' must be at least ${spec.min}` });
          }
          if (typeof spec.max === "number" && value > spec.max) {
            return res.status(400).json({ error: `'${key}' must be at most ${spec.max}` });
          }
          if (spec.type === "int" && !Number.isInteger(value)) {
            return res.status(400).json({ error: `'${key}' must be a whole number` });
          }
        }
        if (spec.type === "bool" && typeof value !== "boolean") {
          return res.status(400).json({ error: `'${key}' must be true or false` });
        }
      }
    }
  }

  const isLlm = baseModel.domain === "llm";
  const isSpeechToText = baseModel.task === "Speech-to-text";
  if (isSpeechToText) {
    // Whisper learns from audio paired with its transcript: a ZIP of audio files plus
    // metadata.csv (file, transcript). A single audio file has nothing to learn from.
    if (body.fineTuneMethod !== "lora" || !body.loraParams) {
      return res.status(400).json({ error: "speech-to-text fine-tuning uses LoRA — send fineTuneMethod 'lora' with loraParams" });
    }
    const snap = (body.datasetVersion
      ? await pool.query("SELECT format, profile FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2", [body.datasetName, body.datasetVersion])
      : await pool.query("SELECT format, profile FROM dataset_snapshots WHERE dataset_name = $1 ORDER BY version DESC LIMIT 1", [body.datasetName])).rows[0];
    if (!snap) return res.status(422).json({ error: `dataset '${body.datasetName}' has no uploaded file yet — upload a ZIP of audio files with a metadata.csv of transcripts` });
    const types = (snap.profile?.fileTypes ?? {}) as Record<string, number>;
    const audioCount = ["wav", "flac", "ogg", "mp3"].reduce((n, e) => n + (types[e] ?? 0), 0);
    if (snap.format !== "zip" || !types.csv || audioCount < 10) {
      return res.status(422).json({
        error: snap.format === "wav"
          ? `dataset '${body.datasetName}' is a single WAV file — it has no transcript to learn from. Fine-tuning needs a ZIP of audio files plus metadata.csv (file, transcript). A single WAV is for testing: open the model and use "Test this model".`
          : `dataset '${body.datasetName}' needs to be a ZIP of at least 10 audio files plus metadata.csv (file, transcript) — this one is ${String(snap.format).toUpperCase()}${snap.format === "zip" ? ` with ${audioCount} audio file(s)${types.csv ? "" : " and no metadata.csv"}` : ""}`,
      });
    }
  } else if (isLlm) {
    if (!body.fineTuneMethod) {
      return res.status(400).json({ error: "fineTuneMethod is required for an LLM base model" });
    }
    if ((body.fineTuneMethod === "lora" || body.fineTuneMethod === "qlora") && !body.loraParams) {
      return res.status(400).json({ error: `loraParams is required when fineTuneMethod is '${body.fineTuneMethod}'` });
    }
    if (body.fineTuneMethod === "full" && !body.fullParams) {
      return res.status(400).json({ error: "fullParams is required when fineTuneMethod is 'full'" });
    }
    if (body.targetColumn || body.features) {
      return res.status(400).json({ error: "targetColumn/features don't apply to an LLM fine-tuning run — this base model has no tabular schema" });
    }
    // A prompt/completion CSV or JSONL is a structured upload too, so the
    // dataset's type alone can't tell a fine-tuning corpus from tabular data.
    // What does is the run naming the text columns it trains on — those are
    // checked against the real snapshot below. Only refuse a structured
    // dataset when nothing says which columns hold training text.
    // An unstructured dataset of documents (PDF, Markdown, text, HTML, Word…) trains in
    // documents mode: text is extracted into passages, so there are no columns to name.
    if (dataset.structure_type === "unstructured") {
      const active = (await pool.query(
        `SELECT DISTINCT ON (file_name) format, profile FROM dataset_snapshots WHERE dataset_name = $1 ORDER BY file_name, version DESC`,
        [body.datasetName])).rows;
      if (!active.length) return res.status(422).json({ error: `dataset '${body.datasetName}' has no uploaded files yet` });
      const DOC_TYPES = ["pdf", "md", "mdx", "txt", "rst", "html", "htm", "docx", "pptx", "markdown"];
      const docCount = active.reduce((n, a) => n + (DOC_TYPES.includes(a.format) ? 1
        : Object.entries((a.profile?.fileTypes ?? {}) as Record<string, number>).reduce((m, [ext, c]) => m + (DOC_TYPES.includes(ext) ? c : 0), 0)), 0);
      if (!docCount) {
        return res.status(422).json({ error: `dataset '${body.datasetName}' has no documents to learn from — LLM fine-tuning on an unstructured dataset needs PDF, Markdown, text, HTML, Word or PowerPoint files` });
      }
      body.llmFineTuneConfig = { textColumn: "text", maxSeqLength: body.llmFineTuneConfig?.maxSeqLength ?? 1024,
        maxPassages: body.llmFineTuneConfig?.maxPassages, fromDocuments: true };
    }
    if (dataset.structure_type !== "unstructured" && !body.llmFineTuneConfig) {
      return res.status(422).json({
        error: `dataset '${body.datasetName}' is structured/tabular — choose its prompt and completion columns (or a single text column) to fine-tune on it`,
      });
    }
    // Real dataset contract — required whenever a real snapshot exists.
    // Unlike time-series (which can fall back to a real synthetic
    // series), there's no honest synthetic-text fallback for LLM
    // fine-tuning, so this is required, not conditionally required.
    const snapshotRow = body.datasetVersion
      ? await pool.query("SELECT column_names FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2", [body.datasetName, body.datasetVersion])
      : await pool.query("SELECT column_names FROM dataset_snapshots WHERE dataset_name = $1 ORDER BY version DESC LIMIT 1", [body.datasetName]);
    if (snapshotRow.rows.length === 0) {
      return res.status(422).json({ error: `dataset '${body.datasetName}' has no real uploaded snapshot yet — LLM fine-tuning needs real training text, there's no synthetic fallback for it` });
    }
    if (!body.llmFineTuneConfig) {
      return res.status(400).json({ error: "llmFineTuneConfig (textColumn, or promptColumn+completionColumn) is required — real fine-tuning needs to know which real column(s) hold the actual training text" });
    }
    const realColumns: string[] = body.llmFineTuneConfig.fromDocuments ? ["text"] : (snapshotRow.rows[0].column_names || []);
    const namedColumns = [body.llmFineTuneConfig.textColumn, body.llmFineTuneConfig.promptColumn, body.llmFineTuneConfig.completionColumn].filter((c): c is string => !!c);
    const missingCols = namedColumns.filter((c) => !realColumns.includes(c));
    if (missingCols.length > 0) {
      return res.status(422).json({ error: `llmFineTuneConfig names column(s) that don't exist in the real dataset: ${missingCols.join(", ")} — real columns are: ${realColumns.join(", ")}` });
    }
  } else {
    if (body.fineTuneMethod || body.loraParams || body.fullParams || body.llmFineTuneConfig) {
      return res.status(400).json({ error: "fineTuneMethod/loraParams/fullParams/llmFineTuneConfig only apply to LLM base models" });
    }
  }

  // Real time-series contract validation — required when a real
  // uploaded snapshot exists for this dataset AND the base model is one
  // of the 6 true time-series-only models, since without it real
  // training has no way to know which column is time, which is the
  // value, or how to handle real gaps. Not required when no real
  // snapshot exists yet — synthetic fallback needs no contract, and
  // requiring one for a dataset that's still metadata-only would be
  // real friction for no real benefit.
  const DEEP_TIME_SERIES_IDS = new Set(["lstm-forecaster", "gru-forecaster", "temporal-fusion-transformer", "nbeats-forecaster"]);
  const needsTimeSeriesContract = TIME_SERIES_ONLY_IDS.has(body.baseModelId) || DEEP_TIME_SERIES_IDS.has(body.baseModelId);
  if (needsTimeSeriesContract) {
    if (body.timeSeriesConfig) {
      const snapshotRow = body.datasetVersion
        ? await pool.query("SELECT column_names FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2", [body.datasetName, body.datasetVersion])
        : await pool.query("SELECT column_names FROM dataset_snapshots WHERE dataset_name = $1 ORDER BY version DESC LIMIT 1", [body.datasetName]);
      if (snapshotRow.rows.length === 0) {
        return res.status(422).json({ error: `timeSeriesConfig was provided, but dataset '${body.datasetName}' has no real uploaded snapshot yet — nothing real to resolve the contract against` });
      }
      const realColumns: string[] = snapshotRow.rows[0].column_names || [];
      const missingCols = [body.timeSeriesConfig.timestampColumn, body.timeSeriesConfig.valueColumn, body.timeSeriesConfig.entityColumn]
        .filter((c): c is string => !!c)
        .filter((c) => !realColumns.includes(c));
      if (missingCols.length > 0) {
        return res.status(422).json({ error: `timeSeriesConfig names column(s) that don't exist in the real dataset: ${missingCols.join(", ")} — real columns are: ${realColumns.join(", ")}` });
      }
      if (body.timeSeriesConfig.entityColumn && !body.timeSeriesConfig.entityValue && !DEEP_TIME_SERIES_IDS.has(body.baseModelId)) {
        return res.status(400).json({ error: "this classical time-series model trains one series at a time — provide entityValue; native PyTorch recurrent/TFT models can instead set globalModel=true" });
      }
      if (body.timeSeriesConfig.entityColumn && !body.timeSeriesConfig.entityValue && DEEP_TIME_SERIES_IDS.has(body.baseModelId)) { body.timeSeriesConfig.globalModel = true; }
      const covariates = [...(body.timeSeriesConfig.knownRealColumns||[]), ...(body.timeSeriesConfig.unknownRealColumns||[]), ...(body.timeSeriesConfig.knownCategoricalColumns||[]), ...(body.timeSeriesConfig.staticCategoricalColumns||[])];
      const missingCovariates = covariates.filter((c) => !realColumns.includes(c));
      if (missingCovariates.length) return res.status(422).json({ error: `timeSeriesConfig covariate column(s) do not exist: ${missingCovariates.join(", ")}` });
    } else {
      const snapshotCheck = await pool.query("SELECT 1 FROM dataset_snapshots WHERE dataset_name = $1 LIMIT 1", [body.datasetName]);
      if (snapshotCheck.rows.length > 0) {
        return res.status(400).json({
          error: `dataset '${body.datasetName}' has a real uploaded snapshot, and '${body.baseModelId}' is a true time-series model — timeSeriesConfig (timestampColumn, valueColumn, and optionally entityColumn/entityValue/frequency/timezone/missingPeriodPolicy) is required to train on it for real`,
        });
      }
    }
  } else if (body.timeSeriesConfig) {
    return res.status(400).json({ error: "timeSeriesConfig only applies to forecasting base models that consume chronological series data" });
  }

  // Real target-column requirement for supervised deep-learning
  // architectures — pytorch_trainer.py's _preprocess_tabular already
  // fails closed on a missing target, but only after a real dataset has
  // been resolved and downloaded. Checked here too, matching the same
  // real requirement POST /api/hpo-searches already enforces for these
  // exact model IDs, so a request without a real target column is
  // rejected immediately instead of discovered deeper into a real (if
  // wasted) download.
  const SUPERVISED_DL_IDS = new Set(["deep-mlp-classifier", "deep-mlp-regressor", "gru-classifier", "cnn-image-classifier"]);
  if (SUPERVISED_DL_IDS.has(body.baseModelId) && !body.targetColumn) {
    return res.status(400).json({ error: `targetColumn is required for supervised deep-learning model '${body.baseModelId}'` });
  }

  const id = randomUUID();
  const createdAt = new Date().toISOString();

  if (body.experimentId) {
    const expCheck = await pool.query("SELECT id FROM experiments WHERE id = $1", [body.experimentId]);
    if (expCheck.rows.length === 0) return res.status(404).json({ error: `experiment '${body.experimentId}' not found` });
  }

  const insertResult = await pool.query(
    `INSERT INTO training_runs
      (id, base_model_id, model_name, dataset_name, compute_profile, status, created_at,
       experiment_id, target_column, features, split_train, split_val, split_test,
       fine_tune_method, lora_params, full_params, requested_dataset_version, time_series_config, llm_fine_tune_config,
       hyperparams, preprocessing, speech_config)
     VALUES ($1, $2, $3, $4, $5, 'queued', $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21)
     RETURNING *`,
    [
      id,
      body.baseModelId,
      body.modelName,
      body.datasetName,
      body.computeProfile,
      createdAt,
      body.experimentId ?? null,
      body.targetColumn ?? null,
      body.features ? JSON.stringify(body.features) : null,
      body.splitTrain ?? null,
      body.splitVal ?? null,
      body.splitTest ?? null,
      body.fineTuneMethod ?? null,
      body.loraParams ? JSON.stringify(body.loraParams) : null,
      body.fullParams ? JSON.stringify(body.fullParams) : null,
      body.datasetVersion ?? null,
      body.timeSeriesConfig ? JSON.stringify(body.timeSeriesConfig) : null,
      body.llmFineTuneConfig ? JSON.stringify(body.llmFineTuneConfig) : null,
      body.hyperparams ? JSON.stringify(body.hyperparams) : null,
      body.preprocessing ? JSON.stringify(body.preprocessing) : null,
      isSpeechToText && body.speechConfig ? JSON.stringify(body.speechConfig) : null,
    ]
  );

  // Real UCS registration now happens once, atomically, in
  // registerTrainedModel() when the run completes — using model_name
  // (what it's registered as) as the identity, not base_model_id (what
  // it was trained from). Registering at creation time, before training
  // has even succeeded, never made sense — an earlier pass here did that
  // with the wrong identity besides; removed rather than fixed in place.

  res.status(201).json(toRunDto(insertResult.rows[0]));
}

trainingRunsRouter.post("/training-runs", requirePermission(PERM.train), requireUcsTrainingWrite, createTrainingRun);

/** The request a stored run was created from — the same fields POST /training-runs
 *  takes, with nulls dropped (the schema's optionals reject null). The dataset is
 *  pinned to the snapshot the run actually read, so a re-train trains on identical
 *  data even if newer versions have been uploaded since. */
export function requestFromRun(row: Record<string, any>): Record<string, unknown> {
  const out: Record<string, unknown> = {
    baseModelId: row.base_model_id,
    modelName: row.model_name,
    datasetName: row.dataset_name,
    datasetVersion: row.dataset_snapshot_version ?? row.requested_dataset_version,
    computeProfile: row.compute_profile,
    experimentId: row.experiment_id,
    targetColumn: row.target_column,
    features: row.features,
    splitTrain: row.split_train,
    splitVal: row.split_val,
    splitTest: row.split_test,
    hyperparams: row.hyperparams,
    preprocessing: row.preprocessing,
    fineTuneMethod: row.fine_tune_method,
    loraParams: row.lora_params,
    fullParams: row.full_params,
    timeSeriesConfig: row.time_series_config,
    llmFineTuneConfig: row.llm_fine_tune_config,
  };
  return Object.fromEntries(Object.entries(out).filter(([, v]) => v !== null && v !== undefined));
}

// POST /api/training-runs/:id/retrain — start a new run with a failed run's exact
// configuration. The failed run is left as it is: its error and log stay readable.
// The permission check runs inline, after the lookup: what it authorises (the model
// being written) is only known once the stored run has been read.
trainingRunsRouter.post("/training-runs/:id/retrain", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const found = await pool.query("SELECT * FROM training_runs WHERE id = $1", [req.params.id]);
  if (found.rows.length === 0) return res.status(404).json({ error: `training run '${req.params.id}' not found` });
  const run = found.rows[0];
  if (run.status !== "failed") {
    return res.status(409).json({ error: `only a failed run can be re-trained — this one is ${run.status}` });
  }
  // A second click, or another person, must not queue the same model twice.
  const inFlight = await pool.query(
    "SELECT id FROM training_runs WHERE model_name = $1 AND status IN ('queued', 'running') LIMIT 1",
    [run.model_name]
  );
  if (inFlight.rows.length) {
    return res.status(409).json({ error: `'${run.model_name}' is already training`, runId: inFlight.rows[0].id });
  }
  req.body = requestFromRun(run);
  // The same UCS check POST /training-runs makes, against the same object
  // (the route already required mlstudio:train:run).
  return requireUcsTrainingWrite(req, res, () => { void createTrainingRun(req, res); });
});

// POST /api/training-runs/:id/complete — manual trigger, kept alongside
// the real background worker for explicit/backward-compat use. Uses the
// SAME atomic claim the worker does (an UPDATE guarded by status IN
// ('queued','running') AND NOT already claimed) so a manual call and the
// worker can never both process the same run — whichever gets there
// first wins the claim; the other sees 0 rows updated and reports the
// real reason rather than double-registering the same model.
trainingRunsRouter.post("/training-runs/:id/complete", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const existing = await pool.query("SELECT * FROM training_runs WHERE id = $1", [req.params.id]);
  if (existing.rows.length === 0) return res.status(404).json({ error: "run not found" });
  if (!["queued", "running"].includes(existing.rows[0].status)) {
    return res.status(409).json({ error: "only a queued or already-claimed-but-not-yet-completed run can be completed" });
  }

  const ucsCheck = await requireUcsModelWrite(req, existing.rows[0].model_name);
  if (!ucsCheck.allowed) return res.status(403).json({ error: ucsCheck.reason });

  const claim = await pool.query(
    `UPDATE training_runs SET status = 'running', claimed_by = $1, claimed_at = COALESCE(claimed_at, NOW()), heartbeat_at = NOW()
     WHERE id = $2 AND status IN ('queued', 'running') AND (claimed_by IS NULL OR claimed_by = $1)
     RETURNING *`,
    [`manual:${req.actor}`, req.params.id]
  );
  if (claim.rows.length === 0) {
    return res.status(409).json({ error: "this run is already claimed by the background worker — it's being processed automatically, nothing to do here" });
  }

  let registrationWarning: string | undefined;
  let qualityGateWarnings: string[] | undefined;
  try {
    const result = await completeTrainingRun(claim.rows[0], req.actor);
    registrationWarning = result.registrationWarning;
    qualityGateWarnings = result.qualityGateWarnings;
  } catch (err) {
    // A real quality-gate blocker — completeTrainingRun already marked
    // the run 'failed' with the real reason; report it as a real 422,
    // not an unhandled 500.
    const finalRow = await pool.query("SELECT * FROM training_runs WHERE id = $1", [req.params.id]);
    return res.status(422).json({ ...toRunDto(finalRow.rows[0]), error: err instanceof Error ? err.message : String(err) });
  }
  const finalRow = await pool.query("SELECT * FROM training_runs WHERE id = $1", [req.params.id]);
  res.json({ ...toRunDto(finalRow.rows[0]), registrationWarning, qualityGateWarnings });
});

// POST /api/training-runs/:id/cancel — a queued run is finished on the spot; a
// running run's trainer subprocess is terminated (here, or by its worker's next
// heartbeat when the worker is a separate process) and the run ends as cancelled.
trainingRunsRouter.post("/training-runs/:id/cancel", requirePermission(PERM.train), async (req: Request, res: Response) => {
  // A queued run has no worker to notice the flag — the claim skips cancelled runs —
  // so it would sit "queued" forever and keep its model under In-training. Finish it
  // here, in the same statement. A running run keeps the flag and stops after its
  // current step, as before.
  const result = await pool.query(
    `UPDATE training_runs
        SET cancel_requested = TRUE,
            status = CASE WHEN status = 'queued' THEN 'failed' ELSE status END,
            error  = CASE WHEN status = 'queued' THEN 'CANCELLED: cancelled before training started' ELSE error END
      WHERE id = $1 AND status IN ('queued', 'running') RETURNING *`,
    [req.params.id]
  );
  if (result.rows.length === 0) return res.status(409).json({ error: "run not found, or already in a terminal state (succeeded/failed)" });
  // Stop the trainer now when it runs in this process; a separate worker process
  // stops it on its next heartbeat (≤3 s), when it sees cancel_requested.
  const stopped = killRunProcesses(req.params.id);
  res.json({ ok: true, id: req.params.id, status: result.rows[0].status, processesStopped: stopped });
});

