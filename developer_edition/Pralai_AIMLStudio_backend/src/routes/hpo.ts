import { Router, Request, Response } from "express";
import { captureReproducibility } from "../services/reproducibility";
import { z } from "zod";
import { randomUUID } from "crypto";
import { promises as fs } from "fs";
import path from "path";
import { pool } from "../db/connection";
import { PERM, requireAnyPermission, requirePermission, requireUcsTrainingWrite } from "../middleware/permissions";
import { getClassicalClasspath, mapToClassicalTask, TIME_SERIES_LIBRARY } from "../services/classicalModelMapping";
import { registerTrainedModel, resolveDatasetSnapshot, getTargetAwareProfile, checkDatasetQualityGate } from "../services/modelRegistration";
import { finalizeSearch } from "../services/hpoWorker";

export const hpoRouter = Router();

export function isDeepLearningFramework(framework: string): boolean {
  return /pytorch|torch|tensorflow|keras|jax/i.test(framework);
}

export function hashStringToInt(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return h;
}

// Deterministic mixed-radix sampler — treats trialIndex as a number in a
// mixed base defined by each parameter's option count, so it enumerates
// genuinely distinct combinations (like a real grid search) rather than
// random sampling. The previous stride-based version had a real bug: a
// stride of (i+3) against a 3-option parameter collapses to index 0 for
// EVERY trial (any multiple of 3 mod 3 is 0) — hiddenLayers silently
// never varied. This is immune to that class of bug by construction.
export function sampleHyperparams(searchSpace: Record<string, unknown[]>, trialIndex: number): Record<string, unknown> {
  const keys = Object.keys(searchSpace);
  const combo: Record<string, unknown> = {};
  let remaining = trialIndex;
  for (const k of keys) {
    const values = searchSpace[k];
    const idx = remaining % values.length;
    combo[k] = values[idx];
    remaining = Math.floor(remaining / values.length);
  }
  return combo;
}

function toSearchSummaryDto(row: any) {
  return {
    id: row.id,
    experimentId: row.experiment_id ?? undefined,
    baseModelId: row.base_model_id,
    datasetName: row.dataset_name,
    searchSpace: row.search_space,
    numTrials: row.num_trials,
    objectiveMetric: row.objective_metric,
    objectiveDirection: row.objective_direction,
    status: row.status,
    bestTrialId: row.best_trial_id,
    computeProfile: row.compute_profile,
    requestedDatasetVersion: row.requested_dataset_version ?? undefined,
    datasetSnapshotVersion: row.dataset_snapshot_version ?? undefined,
    cancelRequested: row.cancel_requested ?? false,
    error: row.error ?? undefined,
    createdAt: row.created_at,
    startedAt: row.started_at ?? undefined,
    completedAt: row.completed_at ?? undefined,
  };
}

hpoRouter.get("/hpo-searches", requireAnyPermission(PERM.experiments, PERM.pipelines, PERM.train), async (_req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM hpo_searches ORDER BY created_at DESC");
  res.json(result.rows.map(toSearchSummaryDto));
});

hpoRouter.get("/hpo-searches/:id", requireAnyPermission(PERM.experiments, PERM.pipelines, PERM.train), async (req: Request, res: Response) => {
  const searchResult = await pool.query("SELECT * FROM hpo_searches WHERE id = $1", [req.params.id]);
  if (searchResult.rows.length === 0) return res.status(404).json({ error: "search not found" });
  const trialsResult = await pool.query("SELECT * FROM hpo_trials WHERE search_id = $1 ORDER BY trial_number ASC", [req.params.id]);
  res.json({
    ...toSearchSummaryDto(searchResult.rows[0]),
    trials: trialsResult.rows.map((t) => ({
      id: t.id, trialNumber: t.trial_number, hyperparams: t.hyperparams, metricValue: t.metric_value,
      dataSource: t.data_source, status: t.status, error: t.error ?? undefined, isBest: t.id === searchResult.rows[0].best_trial_id,
      computeProfile: t.compute_profile, attemptCount: t.attempt_count, maxAttempts: t.max_attempts,
      claimedBy: t.claimed_by ?? undefined, heartbeatAt: t.heartbeat_at ?? undefined,
    })),
  });
});

const createSearchSchema = z.object({
  experimentId: z.string().uuid().optional(),
  baseModelId: z.string(),
  datasetName: z.string(),
  // Real reproducibility pin, same real semantics as training_runs —
  // omitted means the real latest snapshot at search time.
  datasetVersion: z.number().int().positive().optional(),
  // Required to resolve a real dataset for classical/DL trials — HPO
  // previously never even asked for this, which is part of why trials
  // silently trained on synthetic data regardless of what datasetName
  // named: there was no real column to train against even if the
  // dataset itself had been resolved.
  targetColumn: z.string().optional(),
  // Required to resolve a real dataset for the 6 true time-series base
  // models — same real contract training_runs.timeSeriesConfig uses.
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
  // Required to resolve a real dataset for LLM fine-tuning searches —
  // same real contract training_runs.llmFineTuneConfig uses. There's no
  // synthetic-text fallback, so an LLM search with no real dataset
  // simply can't run any real trials.
  fineTuneMethod: z.enum(["lora", "qlora", "full"]).optional(),
  llmFineTuneConfig: z.object({
    textColumn: z.string().optional(),
    promptColumn: z.string().optional(),
    completionColumn: z.string().optional(),
    maxSeqLength: z.number().int().positive().max(8192).optional(),
  }).refine((c) => !!c.textColumn || !!(c.promptColumn && c.completionColumn), {
    message: "llmFineTuneConfig needs either textColumn, or both promptColumn and completionColumn",
  }).optional(),
  searchSpace: z.record(z.array(z.union([z.string(), z.number(), z.array(z.number())]))),
  numTrials: z.number().int().min(1).max(20),
  // Real per-model-type metric sets — accuracy/f1/mse/r2 are what
  // classical_trainer.py/real_trainer.py report; mae/rmse/mape are what
  // timeseries_trainer.py reports; evalLoss/perplexity are what
  // llm_finetune_trainer.py reports. Previously this only allowed the
  // first set, meaning a real time-series (or LLM) HPO search could
  // never declare a metric its own trainer would ever actually
  // produce. Enforced per-base-model below, once the base model is
  // known, not just accepted here regardless of fit.
  objectiveMetric: z.enum(["accuracy", "f1", "mse", "r2", "silhouetteScore", "flaggedRate", "detectionRate", "trustworthiness", "mae", "rmse", "mape", "reconstructionMse", "anomalyRate", "evalLoss", "perplexity"]),
  objectiveDirection: z.enum(["maximize", "minimize"]),
});

// POST /api/hpo-searches — generates real trials (a genuinely different
// hyperparameter combination each), running REAL training per trial —
// real_trainer.py for deep-learning base models, classical_trainer.py
// for the 60 classical-ML base models it covers (dynamically fitting the
// actual sklearn/xgboost/lightgbm/catboost/umap class with THIS trial's
// hyperparameters), timeseries_trainer.py for the 6 true time-series
// base models, and llm_finetune_trainer.py for LLM fine-tuning (real
// LoRA/QLoRA/full per trial — genuinely expensive on real hardware,
// same as a single real training run, just repeated numTrials times;
// this isn't cut down to fewer epochs or a smaller real fit to make HPO
// cheaper, since that would just be a different, undisclosed kind of
// simulation). Execution is durable and asynchronous: this endpoint only
// validates, pins the dataset, and enqueues trial jobs.
hpoRouter.post("/hpo-searches", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const parsed = createSearchSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid HPO search request", details: parsed.error.flatten() });
  const body = parsed.data;

  const baseModelResult = await pool.query("SELECT * FROM base_models WHERE id = $1", [body.baseModelId]);
  if (baseModelResult.rows.length === 0) return res.status(404).json({ error: `base model '${body.baseModelId}' not found` });
  const baseModel = baseModelResult.rows[0];
  if (baseModel.id === "cnn-image-classifier" && !body.targetColumn) body.targetColumn = "label";
  const isLlm = baseModel.domain === "llm";
  const isDeepLearning = !isLlm && baseModel.domain === "ml" && isDeepLearningFramework(baseModel.framework);
  const classicalClasspath = !isLlm && !isDeepLearning ? getClassicalClasspath(baseModel.id, baseModel.uri) : null;
  const classicalTask = classicalClasspath ? mapToClassicalTask(baseModel.task) : null;
  const timeSeriesLibrary = !isLlm && !isDeepLearning && !classicalClasspath ? TIME_SERIES_LIBRARY[baseModel.id] : null;

  // Real per-model-type metric validation — the metric declared has to
  // be one this base model's own real trainer can actually produce, or
  // every real trial fails at the finish line for asking a real trainer
  // for a number it was never going to report.
  const TIME_SERIES_METRICS = new Set(["mae", "rmse", "mape"]);
  const CLASSIFICATION_METRICS = new Set(["accuracy", "f1"]);
  const REGRESSION_METRICS = new Set(["mse", "r2"]);
  const DL_RECONSTRUCTION_METRICS = new Set(["reconstructionMse", "anomalyRate"]);
  const LLM_METRICS = new Set(["evalLoss", "perplexity"]);
  if (timeSeriesLibrary && !TIME_SERIES_METRICS.has(body.objectiveMetric)) {
    return res.status(400).json({ error: `'${body.objectiveMetric}' isn't a real metric ${body.baseModelId} reports — time-series searches use mae, rmse, or mape` });
  }
  if (isLlm && !LLM_METRICS.has(body.objectiveMetric)) {
    return res.status(400).json({ error: `'${body.objectiveMetric}' isn't a real metric ${body.baseModelId} reports — LLM fine-tuning searches use evalLoss or perplexity` });
  }
  const deepForecast = isDeepLearning && ["lstm-forecaster", "gru-forecaster", "nbeats-forecaster", "temporal-fusion-transformer"].includes(baseModel.id);
  const deepReconstruction = isDeepLearning && ["autoencoder-anomaly", "lstm-autoencoder", "denoising-autoencoder", "tabular-embedding-nn"].includes(baseModel.id);
  const deepRegression = isDeepLearning && baseModel.id === "deep-mlp-regressor";
  const deepClassification = isDeepLearning && ["deep-mlp-classifier", "gru-classifier", "cnn-image-classifier"].includes(baseModel.id);
  if (deepForecast && !TIME_SERIES_METRICS.has(body.objectiveMetric)) {
    return res.status(400).json({ error: `'${body.objectiveMetric}' isn't a real metric ${body.baseModelId} reports — deep forecasting searches use mae, rmse, or mape` });
  }
  if (deepForecast && !body.timeSeriesConfig) {
    return res.status(400).json({ error: `timeSeriesConfig is required for ${body.baseModelId} — native recurrent/N-BEATS/TFT training needs a real timestamp/value contract` });
  }
  if (timeSeriesLibrary && body.timeSeriesConfig?.entityColumn && !body.timeSeriesConfig.entityValue) {
    return res.status(400).json({ error: "classical time-series HPO trains one series at a time — provide entityValue; global multi-series is supported by native PyTorch recurrent/TFT models" });
  }
  if (deepForecast && body.timeSeriesConfig?.entityColumn && !body.timeSeriesConfig.entityValue) body.timeSeriesConfig.globalModel = true;
  if (deepReconstruction && !DL_RECONSTRUCTION_METRICS.has(body.objectiveMetric)) {
    return res.status(400).json({ error: `'${body.objectiveMetric}' isn't a real metric ${body.baseModelId} reports — autoencoder/embedding searches use reconstructionMse or anomalyRate` });
  }
  if (deepRegression && !REGRESSION_METRICS.has(body.objectiveMetric)) {
    return res.status(400).json({ error: `'${body.objectiveMetric}' isn't a real metric ${body.baseModelId} reports — this deep regressor uses mse or r2` });
  }
  if (deepClassification && !CLASSIFICATION_METRICS.has(body.objectiveMetric)) {
    return res.status(400).json({ error: `'${body.objectiveMetric}' isn't a real metric ${body.baseModelId} reports — this deep classifier uses accuracy or f1` });
  }
  if ((deepClassification || deepRegression) && !body.targetColumn) {
    return res.status(400).json({ error: `targetColumn is required for supervised deep-learning model ${body.baseModelId}` });
  }
  if (classicalClasspath) {
    const TASK_METRICS: Record<string, Set<string>> = {
      classification: CLASSIFICATION_METRICS,
      regression: REGRESSION_METRICS,
      clustering: new Set(["silhouetteScore"]),
      anomaly_detection: new Set(["flaggedRate", "detectionRate"]),
      dimensionality_reduction: new Set(["trustworthiness"]),
    };
    const allowed = classicalTask ? TASK_METRICS[classicalTask] : null;
    if (!allowed || !allowed.has(body.objectiveMetric)) {
      return res.status(400).json({
        error: allowed
          ? `'${body.objectiveMetric}' isn't a real metric a real ${classicalTask} model reports — use ${Array.from(allowed).join(" or ")}`
          : `${body.baseModelId}'s declared task '${baseModel.task}' isn't a real task classical_trainer.py covers`,
      });
    }
  }
  if (isLlm && !body.fineTuneMethod) {
    return res.status(400).json({ error: "fineTuneMethod (lora, qlora, or full) is required for an LLM base model" });
  }
  if (isLlm && !body.llmFineTuneConfig) {
    return res.status(400).json({ error: "llmFineTuneConfig (textColumn, or promptColumn+completionColumn) is required — real fine-tuning needs to know which real column(s) hold the actual training text" });
  }
  if (isLlm && body.llmFineTuneConfig) {
    const snapshotRow = body.datasetVersion
      ? await pool.query("SELECT column_names FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2", [body.datasetName, body.datasetVersion])
      : await pool.query("SELECT column_names FROM dataset_snapshots WHERE dataset_name = $1 ORDER BY version DESC LIMIT 1", [body.datasetName]);
    if (snapshotRow.rows.length === 0) {
      return res.status(422).json({ error: `dataset '${body.datasetName}' has no real uploaded snapshot yet — LLM fine-tuning has no synthetic-text fallback` });
    }
    const realColumns: string[] = snapshotRow.rows[0].column_names || [];
    const namedColumns = [body.llmFineTuneConfig.textColumn, body.llmFineTuneConfig.promptColumn, body.llmFineTuneConfig.completionColumn].filter((c): c is string => !!c);
    const missingCols = namedColumns.filter((c) => !realColumns.includes(c));
    if (missingCols.length > 0) {
      return res.status(422).json({ error: `llmFineTuneConfig names column(s) that don't exist in the real dataset: ${missingCols.join(", ")} — real columns are: ${realColumns.join(", ")}` });
    }
  }
  if ((timeSeriesLibrary || deepForecast) && body.timeSeriesConfig) {
    const snapshotRow = body.datasetVersion
      ? await pool.query("SELECT column_names FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2", [body.datasetName, body.datasetVersion])
      : await pool.query("SELECT column_names FROM dataset_snapshots WHERE dataset_name = $1 ORDER BY version DESC LIMIT 1", [body.datasetName]);
    if (snapshotRow.rows.length > 0) {
      const realColumns: string[] = snapshotRow.rows[0].column_names || [];
      const missingCols = [body.timeSeriesConfig.timestampColumn, body.timeSeriesConfig.valueColumn, body.timeSeriesConfig.entityColumn]
        .filter((c): c is string => !!c)
        .filter((c) => !realColumns.includes(c));
      if (missingCols.length > 0) {
        return res.status(422).json({ error: `timeSeriesConfig names column(s) that don't exist in the real dataset: ${missingCols.join(", ")} — real columns are: ${realColumns.join(", ")}` });
      }
      const covariates=[...(body.timeSeriesConfig.knownRealColumns||[]),...(body.timeSeriesConfig.unknownRealColumns||[]),...(body.timeSeriesConfig.knownCategoricalColumns||[]),...(body.timeSeriesConfig.staticCategoricalColumns||[])];
      const missingCovariates=covariates.filter(c=>!realColumns.includes(c));
      if(missingCovariates.length) return res.status(422).json({error:`timeSeriesConfig covariate column(s) do not exist: ${missingCovariates.join(", ")}`});
    }
  }

  if (body.experimentId) {
    const expCheck = await pool.query("SELECT id FROM experiments WHERE id = $1", [body.experimentId]);
    if (expCheck.rows.length === 0) return res.status(404).json({ error: `experiment '${body.experimentId}' not found` });
  }

  // Real dataset resolution — ONCE, up front, reused across every real
  // trial rather than re-resolved (or, previously, never resolved at
  // all) per trial. A real quality-gate block here fails the WHOLE
  // search before any real compute is spent on trials that would just
  // train on bad data. Genuinely fail-closed the same way a normal
  // training run is: DATASET_UNAVAILABLE and quality-gate failures both
  // throw here, same as resolveDatasetSnapshot/checkDatasetQualityGate
  // already guarantee elsewhere — HPO was the one real training path
  // that had opted out of both by simply never calling either.
  let resolvedDataset: Awaited<ReturnType<typeof resolveDatasetSnapshot>> = null;
  if (!isDeepLearning && classicalClasspath && classicalTask) {
    resolvedDataset = await resolveDatasetSnapshot(pool, body.datasetName, body.datasetVersion ?? null);
    if (resolvedDataset && body.targetColumn) {
      const profile = await getTargetAwareProfile(resolvedDataset, body.targetColumn);
      const gate = checkDatasetQualityGate(profile, body.targetColumn);
      if (gate.blocked) {
        return res.status(422).json({ error: `dataset '${body.datasetName}' v${resolvedDataset.snapshotVersion} failed real quality checks: ${gate.blockers.join("; ")}` });
      }
    }
  } else if (isDeepLearning) {
    resolvedDataset = await resolveDatasetSnapshot(pool, body.datasetName, body.datasetVersion ?? null);
    if (!resolvedDataset) {
      return res.status(422).json({ error: `dataset '${body.datasetName}' has no real uploaded snapshot — native deep-learning training has no synthetic fallback` });
    }
    if (body.targetColumn) {
      const profile = await getTargetAwareProfile(resolvedDataset, body.targetColumn);
      const gate = checkDatasetQualityGate(profile, body.targetColumn);
      if (gate.blocked) {
        return res.status(422).json({ error: `dataset '${body.datasetName}' v${resolvedDataset.snapshotVersion} failed real quality checks: ${gate.blockers.join("; ")}` });
      }
    }
  } else if (timeSeriesLibrary && body.timeSeriesConfig) {
    resolvedDataset = await resolveDatasetSnapshot(pool, body.datasetName, body.datasetVersion ?? null);
  } else if (isLlm) {
    // No synthetic-text fallback — already validated above that a real
    // snapshot exists before we got here, but resolveDatasetSnapshot
    // still throws its own real DATASET_UNAVAILABLE if the download
    // itself fails (a real snapshot row existing doesn't guarantee the
    // real object is still retrievable from storage).
    resolvedDataset = await resolveDatasetSnapshot(pool, body.datasetName, body.datasetVersion ?? null);
    if (!resolvedDataset) {
      return res.status(422).json({ error: `dataset '${body.datasetName}' has no real uploaded snapshot — LLM fine-tuning has no synthetic-text fallback` });
    }
  }

  if (!resolvedDataset) {
    return res.status(422).json({ error: `dataset '${body.datasetName}' has no real uploaded snapshot — durable HPO requires an immutable real dataset snapshot for every trial` });
  }

  const searchId = randomUUID();
  const createdAt = new Date().toISOString();
  const resolvedVersion = resolvedDataset?.snapshotVersion ?? body.datasetVersion ?? null;
  const computeProfile = baseModel.compute_profile ?? (isLlm || isDeepLearning ? "gpu-1x" : "cpu-small");
  const searchReproducibility = await captureReproducibility({ baseModelId: body.baseModelId, baseModelUri: baseModel.uri, computeProfile, datasetName: body.datasetName, datasetSnapshotVersion: resolvedVersion, datasetChecksum: resolvedDataset.checksum, timeSeriesConfig: body.timeSeriesConfig ?? null, fineTuneMethod: body.fineTuneMethod ?? null });

  // v2.36.3: HPO is now a durable queue. The API validates and pins the
  // dataset, materializes one queued row per trial, then returns 202.
  // Workers claim trials with FOR UPDATE SKIP LOCKED and compute-profile
  // routing; no model fitting remains in the HTTP request lifecycle.
  await pool.query(
    `INSERT INTO hpo_searches
      (id, experiment_id, base_model_id, dataset_name, search_space, num_trials, objective_metric, objective_direction, status, created_at,
       requested_dataset_version, dataset_snapshot_version, target_column, time_series_config, fine_tune_method, llm_fine_tune_config, compute_profile, dataset_checksum, reproducibility)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'queued',$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)`,
    [searchId, body.experimentId ?? null, body.baseModelId, body.datasetName, JSON.stringify(body.searchSpace), body.numTrials,
     body.objectiveMetric, body.objectiveDirection, createdAt, body.datasetVersion ?? null, resolvedVersion, body.targetColumn ?? null,
     body.timeSeriesConfig ? JSON.stringify(body.timeSeriesConfig) : null, body.fineTuneMethod ?? null,
     body.llmFineTuneConfig ? JSON.stringify(body.llmFineTuneConfig) : null, computeProfile, resolvedDataset.checksum, JSON.stringify(searchReproducibility)]
  );

  for (let i = 0; i < body.numTrials; i++) {
    const trialId = randomUUID();
    const hyperparams = sampleHyperparams(body.searchSpace, i);
    const seed = Math.abs(hashStringToInt(`${searchId}-${i}`));
    await pool.query(
      `INSERT INTO hpo_trials
        (id, search_id, trial_number, hyperparams, seed, data_source, status, created_at, compute_profile)
       VALUES ($1,$2,$3,$4,$5,'measured','queued',$6,$7)`,
      [trialId, searchId, i + 1, JSON.stringify(hyperparams), seed, createdAt, computeProfile]
    );
  }

  if (resolvedDataset) {
    await fs.rm(path.dirname(resolvedDataset.localPath), { recursive: true, force: true }).catch(() => {});
  }

  const finalSearch = await pool.query("SELECT * FROM hpo_searches WHERE id = $1", [searchId]);
  res.status(202).json({
    ...toSearchSummaryDto(finalSearch.rows[0]),
    message: "HPO search queued; trials will be claimed by background workers",
  });
});

// Durable cancellation: queued trials are cancelled immediately; running
// workers observe cancel_requested via their heartbeat poll and abort the
// Python child process through AbortSignal-aware trainer wrappers.
hpoRouter.post("/hpo-searches/:id/cancel", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const found = await pool.query("SELECT * FROM hpo_searches WHERE id = $1", [req.params.id]);
  if (found.rows.length === 0) return res.status(404).json({ error: "search not found" });
  await pool.query("UPDATE hpo_searches SET cancel_requested = TRUE WHERE id = $1 AND status IN ('queued','running')", [req.params.id]);
  await pool.query("UPDATE hpo_trials SET cancel_requested = TRUE, status = CASE WHEN status='queued' THEN 'cancelled' ELSE status END, completed_at = CASE WHEN status='queued' THEN NOW() ELSE completed_at END WHERE search_id = $1 AND status IN ('queued','running')", [req.params.id]);
  // Real finalization, not just a flag flip. If every real trial was
  // still queued (none had been claimed by a worker yet), the update
  // above already cancelled all of them directly — but nothing else
  // would ever revisit this search to notice that and close it out,
  // since finalizeSearch() otherwise only runs from inside a real
  // worker's own processTrial()/sweepHpoTimeouts() cycle, neither of
  // which ever touches a trial that was cancelled before a worker ever
  // claimed it. Without this, a search cancelled entirely at the
  // "still queued" stage stayed 'queued' forever — its trials real and
  // correctly cancelled, but the search itself a real orphaned status
  // no client would ever see resolve. Safe to call unconditionally:
  // finalizeSearch() itself no-ops if any trial is still genuinely
  // running, correctly leaving that case for the worker to close out
  // once it actually finishes aborting.
  await finalizeSearch(pool, req.params.id);
  res.status(202).json({ id: req.params.id, cancelRequested: true });
});

const promoteBestSchema = z.object({ modelName: z.string().min(1) });

// POST /api/hpo-searches/:id/promote-best — takes the winning trial and
// runs it through the SAME registration pipeline a normal training run
// completion does (registerTrainedModel) — a real catalog_models +
// model_versions + model_monitoring entry, using the winning trial's real
// hyperparameters and real metric, not a re-simulated one.
hpoRouter.post("/hpo-searches/:id/promote-best", requirePermission(PERM.train), requireUcsTrainingWrite, async (req: Request, res: Response) => {
  const parsed = promoteBestSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid request", details: parsed.error.flatten() });

  const searchResult = await pool.query("SELECT * FROM hpo_searches WHERE id = $1", [req.params.id]);
  if (searchResult.rows.length === 0) return res.status(404).json({ error: "search not found" });
  const search = searchResult.rows[0];
  if (!search.best_trial_id) return res.status(422).json({ error: "this search has no completed best trial to promote" });

  const trialResult = await pool.query("SELECT * FROM hpo_trials WHERE id = $1", [search.best_trial_id]);
  const trial = trialResult.rows[0];

  const existingCatalog = await pool.query("SELECT base_ref FROM catalog_models WHERE id = $1", [parsed.data.modelName]);
  if (existingCatalog.rows.length > 0 && existingCatalog.rows[0].base_ref !== search.base_model_id) {
    return res.status(409).json({ error: `'${parsed.data.modelName}' is already registered against a different base model` });
  }

  const runId = randomUUID();
  const metrics = { [search.objective_metric]: trial.metric_value };
  const runResult = await pool.query(
    `INSERT INTO training_runs (id, base_model_id, model_name, dataset_name, compute_profile, status, created_at, experiment_id, metrics, hyperparams, requested_dataset_version, dataset_snapshot_version, target_column, time_series_config, fine_tune_method, llm_fine_tune_config, dataset_checksum, reproducibility)
     VALUES ($1, $2, $3, $4, $5, 'succeeded', $6, $7, $8, $9, $10, $10, $11, $12, $13, $14, $15, $16) RETURNING *`,
    [runId, search.base_model_id, parsed.data.modelName, search.dataset_name, trial.compute_profile || search.compute_profile || 'gpu-1x', new Date().toISOString(), search.experiment_id, JSON.stringify(metrics), trial.hyperparams, search.dataset_snapshot_version, search.target_column, search.time_series_config ? JSON.stringify(search.time_series_config) : null, search.fine_tune_method, search.llm_fine_tune_config ? JSON.stringify(search.llm_fine_tune_config) : null, search.dataset_checksum, trial.reproducibility ? JSON.stringify(trial.reproducibility) : search.reproducibility ? JSON.stringify(search.reproducibility) : null]
  );

  let registrationWarning: string | undefined;
  let measuredMetrics: Record<string, unknown> | null = null;
  const reusingExactArtifact = !!trial.weights_uri;
  try {
    // The actual fix: if this trial produced a real, uploaded artifact
    // (weights_uri set — see the search loop above), reuse it directly
    // via preTrainedWeightsUri instead of training again. Previously
    // this always called registerTrainedModel() with only the winning
    // hyperparameters, which re-trained from scratch — a fresh, real,
    // but not byte-identical model, not the one actually evaluated
    // during the search. Falls back to re-training (still with the real
    // winning hyperparameters) only when no artifact was saved — a
    // simulated trial, or one whose upload failed/wasn't configured.
    const registration = await registerTrainedModel(
      pool,
      {
        ...runResult.rows[0],
        metrics,
        hyperparams: trial.hyperparams,
        preTrainedWeightsUri: trial.weights_uri,
        preTrainedArtifactFormat: trial.artifact_format,
      },
      req.actor
    );
    measuredMetrics = registration.measuredMetrics;
    // Only overwrite metrics with a fresh measurement when training
    // actually ran again — when the exact trial artifact was reused,
    // the trial's own recorded metric IS what this version measured;
    // there's no new number to replace it with.
    if (measuredMetrics && !reusingExactArtifact) {
      await pool.query("UPDATE training_runs SET metrics = $1 WHERE id = $2", [JSON.stringify(measuredMetrics), runId]);
    }
  } catch (err) {
    registrationWarning = `Run recorded, but registering '${parsed.data.modelName}' failed: ${err instanceof Error ? err.message : String(err)}`;
  }

  res.status(201).json({
    reusedExactTrialArtifact: reusingExactArtifact,
    trainingRunId: runId,
    modelName: parsed.data.modelName,
    promotedFromTrial: trial.trial_number,
    metricValue: trial.metric_value,
    measuredMetrics,
    hyperparams: trial.hyperparams,
    registrationWarning,
  });
});
