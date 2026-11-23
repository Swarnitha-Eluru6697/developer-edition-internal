import { Pool } from "pg";
import os from "os";
import { reportWorkerHeartbeat } from "./workerRegistry";
import path from "path";
import { promises as fs } from "fs";
import { runDeepLearningTraining } from "./deepLearningTrainer";
import { runClassicalTraining } from "./classicalTrainer";
import { runTimeSeriesTraining } from "./timeseriesTrainer";
import { runLlmFineTuning } from "./llmFinetuneTrainer";
import { getClassicalClasspath, mapToClassicalTask, TIME_SERIES_LIBRARY } from "./classicalModelMapping";
import { resolveDatasetSnapshot, uploadRealWeights } from "./modelRegistration";
import { captureReproducibility } from "./reproducibility";

const WORKER_ID = process.env.WORKER_ID || `${os.hostname()}-${process.pid}`;
const CONCURRENCY = Math.max(1, Number(process.env.HPO_WORKER_CONCURRENCY || process.env.WORKER_CONCURRENCY || 1));
const PROFILE_ALLOWLIST = (process.env.WORKER_COMPUTE_PROFILES || "").split(",").map(s => s.trim()).filter(Boolean);
const HEARTBEAT_MS = Math.max(1000, Number(process.env.HPO_HEARTBEAT_INTERVAL_MS || 5000));
const STALE_SECONDS = Math.max(30, Number(process.env.HPO_STALE_HEARTBEAT_SECONDS || 120));

function isDeep(framework: string): boolean { return /pytorch|torch|tensorflow|keras|jax/i.test(framework); }

async function claimNextTrial(pool: Pool): Promise<any | null> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const params: unknown[] = [];
    let profileFilter = "";
    if (PROFILE_ALLOWLIST.length) { params.push(PROFILE_ALLOWLIST); profileFilter = `AND t.compute_profile = ANY($${params.length}::text[])`; }
    const q = await client.query(
      `SELECT t.*, s.base_model_id, s.dataset_name, s.dataset_snapshot_version, s.target_column, s.time_series_config,
              s.fine_tune_method, s.llm_fine_tune_config, s.objective_metric, s.objective_direction, s.dataset_checksum, s.reproducibility AS search_reproducibility, s.status AS search_status
       FROM hpo_trials t JOIN hpo_searches s ON s.id=t.search_id
       WHERE t.status='queued' AND NOT t.cancel_requested AND NOT s.cancel_requested AND s.status IN ('queued','running') ${profileFilter}
       ORDER BY t.created_at, t.trial_number
       FOR UPDATE OF t SKIP LOCKED LIMIT 1`, params);
    if (!q.rows.length) { await client.query("COMMIT"); return null; }
    const trial = q.rows[0];
    const updated = await client.query(
      `UPDATE hpo_trials SET status='running', claimed_by=$1, claimed_at=NOW(), heartbeat_at=NOW(), started_at=COALESCE(started_at,NOW()), attempt_count=attempt_count+1
       WHERE id=$2 RETURNING *`, [WORKER_ID, trial.id]);
    await client.query("UPDATE hpo_searches SET status='running', started_at=COALESCE(started_at,NOW()) WHERE id=$1 AND status='queued'", [trial.search_id]);
    await client.query("COMMIT");
    return { ...trial, ...updated.rows[0] };
  } catch (e) { await client.query("ROLLBACK").catch(()=>{}); throw e; } finally { client.release(); }
}

export async function finalizeSearch(pool: Pool, searchId: string): Promise<void> {
  const counts = await pool.query(`SELECT status, COUNT(*)::int n FROM hpo_trials WHERE search_id=$1 GROUP BY status`, [searchId]);
  const map = Object.fromEntries(counts.rows.map(r => [r.status, Number(r.n)]));
  if ((map.queued || 0) + (map.running || 0) > 0) return;
  const search = (await pool.query("SELECT * FROM hpo_searches WHERE id=$1", [searchId])).rows[0];
  if (!search) return;
  if (search.cancel_requested) {
    await pool.query("UPDATE hpo_searches SET status='cancelled', completed_at=NOW() WHERE id=$1", [searchId]);
    return;
  }
  const best = await pool.query(
    `SELECT id FROM hpo_trials WHERE search_id=$1 AND status='completed' AND metric_value IS NOT NULL
     ORDER BY CASE WHEN $2='maximize' THEN metric_value END DESC NULLS LAST,
              CASE WHEN $2='minimize' THEN metric_value END ASC NULLS LAST, trial_number ASC LIMIT 1`, [searchId, search.objective_direction]);
  if (!best.rows.length) {
    await pool.query("UPDATE hpo_searches SET status='failed', error='all HPO trials failed', completed_at=NOW() WHERE id=$1", [searchId]);
  } else {
    await pool.query("UPDATE hpo_searches SET status='completed', best_trial_id=$1, completed_at=NOW() WHERE id=$2", [best.rows[0].id, searchId]);
  }
}

async function executeTrial(pool: Pool, trial: any, signal: AbortSignal): Promise<{metricValue:number|null; weightsUri:string|null; artifactFormat:string|null}> {
  const base = (await pool.query("SELECT * FROM base_models WHERE id=$1", [trial.base_model_id])).rows[0];
  if (!base) throw new Error(`base model '${trial.base_model_id}' not found`);
  const isLlm = base.domain === "llm";
  const deep = !isLlm && base.domain === "ml" && isDeep(base.framework);
  const classpath = !isLlm && !deep ? getClassicalClasspath(base.id, base.uri) : null;
  const task = classpath ? mapToClassicalTask(base.task) : null;
  const tsLibrary = !isLlm && !deep && !classpath ? TIME_SERIES_LIBRARY[base.id] : null;
  const hp = trial.hyperparams || {};
  const ts = trial.time_series_config || null;
  const llm = trial.llm_fine_tune_config || null;
  const resolved = await resolveDatasetSnapshot(pool, trial.dataset_name, trial.dataset_snapshot_version ?? null);
  if (!resolved) throw new Error(`dataset '${trial.dataset_name}' snapshot is unavailable`);
  const key = `hpo-trials/${trial.search_id}/trial-${trial.trial_number}/`;
  try {
    if (deep) {
      const r = await runDeepLearningTraining(base.id, trial.seed, hp, {datasetPath: resolved.localPath, targetColumn: trial.target_column ?? null, datasetFormat: resolved.format, timeSeriesConfig: ts}, trial.compute_profile, signal);
      if (!r.ok) throw new Error(r.error);
      const metric = r.finalMetrics[trial.objective_metric] ?? r.validationMetricValue ?? null;
      if (metric == null) throw new Error(`trainer produced no '${trial.objective_metric}' metric`);
      const uri = await uploadRealWeights(`${key}model-pytorch.tar.gz`, r.modelFilePath);
      return {metricValue:Number(metric), weightsUri:uri, artifactFormat:uri?"pytorch":null};
    }
    if (classpath && task) {
      const r = await runClassicalTraining(classpath, task as any, trial.seed, hp, {datasetPath:resolved.localPath,targetColumn:trial.target_column??null,datasetFormat:resolved.format,features:trial.features??null}, signal);
      if (!r.ok) throw new Error(r.error);
      const metric = (r as any)[trial.objective_metric];
      if (metric == null) throw new Error(`trainer produced no '${trial.objective_metric}' metric`);
      const uri = await uploadRealWeights(`${key}model.joblib`, r.modelFilePath);
      return {metricValue:Number(metric),weightsUri:uri,artifactFormat:uri?"sklearn":null};
    }
    if (tsLibrary && ts) {
      const r = await runTimeSeriesTraining(tsLibrary as any, trial.seed, {datasetPath:resolved.localPath,datasetFormat:resolved.format,timestampColumn:ts.timestampColumn,valueColumn:ts.valueColumn,entityColumn:ts.entityColumn,entityValue:ts.entityValue,frequency:ts.frequency,timezone:ts.timezone,missingPeriodPolicy:ts.missingPeriodPolicy}, signal);
      if (!r.ok) throw new Error(r.error);
      const metric = ({mae:r.mae,rmse:r.rmse,mape:r.mape} as any)[trial.objective_metric];
      if (metric == null) throw new Error(`trainer produced no '${trial.objective_metric}' metric`);
      const uri = r.modelFilePath ? await uploadRealWeights(`${key}model.joblib`, r.modelFilePath) : null;
      return {metricValue:Number(metric),weightsUri:uri,artifactFormat:uri?"sklearn":null};
    }
    if (isLlm && trial.fine_tune_method && llm) {
      const r = await runLlmFineTuning(base.uri, trial.fine_tune_method, trial.seed,
        {datasetPath:resolved.localPath,datasetFormat:resolved.format,textColumn:llm.textColumn,promptColumn:llm.promptColumn,completionColumn:llm.completionColumn,maxSeqLength:llm.maxSeqLength},
        trial.fine_tune_method!=="full"?hp:undefined, trial.fine_tune_method==="full"?hp:undefined, signal);
      if (!r.ok) throw new Error(r.error);
      const metric = trial.objective_metric==="evalLoss"?r.evalLoss:trial.objective_metric==="perplexity"?r.perplexity:null;
      if (metric == null) throw new Error(`trainer produced no '${trial.objective_metric}' metric`);
      const uri = await uploadRealWeights(`${key}${trial.fine_tune_method==="full"?"model.tar.gz":"adapter.tar.gz"}`, r.modelFilePath);
      return {metricValue:Number(metric),weightsUri:uri,artifactFormat:uri?(trial.fine_tune_method==="full"?"huggingface":"huggingface-peft"):null};
    }
    throw new Error(`no real HPO executor is wired for base model '${base.id}'`);
  } finally { await fs.rm(path.dirname(resolved.localPath), {recursive:true,force:true}).catch(()=>{}); }
}

let inFlight = 0;
async function processTrial(pool: Pool, trial: any): Promise<void> {
  const abort = new AbortController();
  const heartbeat = setInterval(async () => {
    await pool.query("UPDATE hpo_trials SET heartbeat_at=NOW() WHERE id=$1 AND claimed_by=$2", [trial.id, WORKER_ID]).catch(()=>{});
    const c = await pool.query("SELECT t.cancel_requested, s.cancel_requested AS search_cancel FROM hpo_trials t JOIN hpo_searches s ON s.id=t.search_id WHERE t.id=$1", [trial.id]).catch(()=>null as any);
    if (c?.rows?.[0] && (c.rows[0].cancel_requested || c.rows[0].search_cancel)) abort.abort();
  }, HEARTBEAT_MS);
  try {
    const out = await executeTrial(pool, trial, abort.signal);
    if (abort.signal.aborted) throw new Error("HPO trial cancelled");
    const repro = await captureReproducibility({ ...(trial.search_reproducibility || {}), hpoSearchId: trial.search_id, hpoTrialId: trial.id, trialNumber: trial.trial_number, seed: trial.seed, hyperparams: trial.hyperparams, datasetChecksum: trial.dataset_checksum ?? null, artifactFormat: out.artifactFormat, weightsUri: out.weightsUri, objectiveMetric: trial.objective_metric, metricValue: out.metricValue });
    await pool.query(`UPDATE hpo_trials SET status='completed', metric_value=$1, data_source='measured', weights_uri=$2, artifact_format=$3, reproducibility=$4, completed_at=NOW(), claimed_by=NULL, claimed_at=NULL, heartbeat_at=NULL WHERE id=$5`, [out.metricValue,out.weightsUri,out.artifactFormat,JSON.stringify(repro),trial.id]);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    const cancelled = abort.signal.aborted || /cancelled|aborted/i.test(msg);
    const fresh = (await pool.query("SELECT attempt_count,max_attempts FROM hpo_trials WHERE id=$1", [trial.id])).rows[0];
    const retry = !cancelled && fresh.attempt_count < fresh.max_attempts;
    await pool.query(`UPDATE hpo_trials SET status=$1, error=$2, completed_at=CASE WHEN $1 IN ('failed','cancelled') THEN NOW() ELSE completed_at END, claimed_by=NULL, claimed_at=NULL, heartbeat_at=NULL WHERE id=$3`, [cancelled?"cancelled":retry?"queued":"failed",msg,trial.id]);
  } finally { clearInterval(heartbeat); await finalizeSearch(pool, trial.search_id); }
}

export async function runHpoWorkerCycle(pool: Pool): Promise<void> {
  await reportWorkerHeartbeat(pool, "hpo", getHpoWorkerStatus());
  const headroom = CONCURRENCY - inFlight;
  for (let i=0;i<headroom;i++) {
    const trial = await claimNextTrial(pool); if (!trial) break;
    inFlight++; processTrial(pool, trial).catch(console.error).finally(()=>inFlight--);
  }
}

export async function sweepHpoTimeouts(pool: Pool): Promise<void> {
  const stale = await pool.query(`SELECT * FROM hpo_trials WHERE status='running' AND heartbeat_at < NOW() - INTERVAL '${STALE_SECONDS} seconds'`);
  for (const t of stale.rows) {
    const retry = t.attempt_count < t.max_attempts;
    await pool.query(`UPDATE hpo_trials SET status=$1,error=$2,claimed_by=NULL,claimed_at=NULL,heartbeat_at=NULL WHERE id=$3 AND status='running'`, [retry?"queued":"failed",`HPO worker heartbeat stale >${STALE_SECONDS}s`,t.id]);
    await finalizeSearch(pool,t.search_id);
  }
}

export function getHpoWorkerStatus(){ return {workerId:WORKER_ID, concurrency:CONCURRENCY, inFlight, computeProfileAllowlist:PROFILE_ALLOWLIST.length?PROFILE_ALLOWLIST:null}; }
