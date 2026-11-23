import "dotenv/config";
import { app } from "./app";
import { initSchema, pool } from "./db/connection";
import { loadReferenceDataAtStartup } from "./db/referenceData";
import { loadSampleDatasetsAtStartup } from "./db/sampleDatasets";
import { evaluateAlerts } from "./routes/alerts";
import { reconcileArgoWorkflows } from "./execution/workflowEngine";
import { runWorkerCycle, sweepTimeouts } from "./services/trainingWorker";
import { runHpoWorkerCycle, sweepHpoTimeouts } from "./services/hpoWorker";
import { collectProductionTelemetry } from "./services/monitoringTelemetry";

const PORT = process.env.PORT ? Number(process.env.PORT) : 4000;
// Real recurring alert evaluation — previously the only way to run
// evaluateAlerts() was a manual POST /api/alerts/evaluate call or an
// external cron; nothing in the process itself ever ran it periodically.
// A real in-process interval, not a separate worker/queue (that's the
// larger, explicitly-deferred "real async job lifecycle" item) — this
// is what "real periodic evaluation" means concretely without one.
// Disable with ALERT_EVAL_INTERVAL_MS=0 for tests/local runs that don't
// want background queries firing.
const ALERT_EVAL_INTERVAL_MS = process.env.ALERT_EVAL_INTERVAL_MS ? Number(process.env.ALERT_EVAL_INTERVAL_MS) : 60_000;
// Real recurring Argo status reconciliation — genuinely a no-op when
// ARGO_SERVER_URL isn't set (reconcileArgoWorkflows() checks the
// configured engine's kind and returns immediately for the local
// executor, which drives its own state transitions directly). Previously
// nothing ever polled Argo for status at all — a "running" pipeline
// against a real cluster would sit unreconciled in the local DB forever.
const ARGO_RECONCILE_INTERVAL_MS = process.env.ARGO_RECONCILE_INTERVAL_MS ? Number(process.env.ARGO_RECONCILE_INTERVAL_MS) : 15_000;
// Real async training worker — genuinely claims queued runs itself
// (a real Postgres FOR UPDATE SKIP LOCKED claim, not a fake stub) rather
// than only ever processing when a client happens to call
// POST .../complete. A separate, less frequent sweep reclaims runs whose
// worker went stale (crashed mid-run).
//
// This in-process interval is the real dev/small-deployment default —
// genuinely fine at that scale, and zero extra processes to run. At
// production scale, run src/worker.ts instead as its own separate OS
// process (or many of them — see that file and k8s/worker-deployment.yaml)
// and set TRAINING_WORKER_INTERVAL_MS=0 here so the API process stops
// also claiming work itself. Both can safely run at once if you don't —
// the real SKIP LOCKED claim was always safe for that — it's just
// redundant, not incorrect, to have the API also competing for work a
// dedicated fleet is already handling.
const TRAINING_WORKER_INTERVAL_MS = process.env.TRAINING_WORKER_INTERVAL_MS ? Number(process.env.TRAINING_WORKER_INTERVAL_MS) : 2_000;
const TRAINING_TIMEOUT_SWEEP_INTERVAL_MS = process.env.TRAINING_TIMEOUT_SWEEP_INTERVAL_MS ? Number(process.env.TRAINING_TIMEOUT_SWEEP_INTERVAL_MS) : 30_000;

async function main() {
  await initSchema();
  // The reference catalog (algorithm specs, base models, compute pools) —
  // awaited, since the Train wizard is empty without it, but it is a few
  // hundred milliseconds of upserts and never throws: a failure is logged and
  // the server starts on whatever catalog the database already holds.
  // SEED_REFERENCE_DATA=false skips it. Never `npm run seed`, which drops
  // every table and is for local development only.
  await loadReferenceDataAtStartup(pool);
  app.listen(PORT, () => {
    // eslint-disable-next-line no-console
    console.log(`ML Studio API listening on http://localhost:${PORT}`);
    // Sample datasets profile and upload real files, which takes seconds —
    // run after listening so readiness probes aren't held up. Never throws.
    // SEED_SAMPLE_DATASETS=false skips it.
    void loadSampleDatasetsAtStartup();
  });
  if (ALERT_EVAL_INTERVAL_MS > 0) {
    setInterval(() => {
      evaluateAlerts().catch((err) => {
        // eslint-disable-next-line no-console
        console.error("Scheduled alert evaluation failed:", err instanceof Error ? err.message : err);
      });
    }, ALERT_EVAL_INTERVAL_MS);
    // eslint-disable-next-line no-console
    console.log(`Real alert evaluation running every ${ALERT_EVAL_INTERVAL_MS}ms`);
  }
  if (ARGO_RECONCILE_INTERVAL_MS > 0) {
    setInterval(() => {
      reconcileArgoWorkflows(pool).catch((err) => {
        // eslint-disable-next-line no-console
        console.error("Argo status reconciliation failed:", err instanceof Error ? err.message : err);
      });
    }, ARGO_RECONCILE_INTERVAL_MS);
  }
  if (TRAINING_WORKER_INTERVAL_MS > 0) {
    setInterval(() => {
      runWorkerCycle(pool).catch((err) => {
        // eslint-disable-next-line no-console
        console.error("Training worker cycle failed:", err instanceof Error ? err.message : err);
      });
      runHpoWorkerCycle(pool).catch((err) => console.error("HPO worker cycle failed:", err instanceof Error ? err.message : err));
    }, TRAINING_WORKER_INTERVAL_MS);
    setInterval(() => {
      sweepTimeouts(pool).catch((err) => {
        // eslint-disable-next-line no-console
        console.error("Training timeout sweep failed:", err instanceof Error ? err.message : err);
      });
      sweepHpoTimeouts(pool).catch((err) => console.error("HPO timeout sweep failed:", err instanceof Error ? err.message : err));
    }, TRAINING_TIMEOUT_SWEEP_INTERVAL_MS);
    // eslint-disable-next-line no-console
    console.log(`Real async training worker running every ${TRAINING_WORKER_INTERVAL_MS}ms (queued runs are now claimed automatically — POST .../complete is no longer required, just kept for explicit/manual use)`);
  }
  const telemetryInterval = Number(process.env.MONITORING_TELEMETRY_INTERVAL_MS || 60000);
  if (telemetryInterval > 0 && (process.env.PROMETHEUS_METRICS_URL || process.env.KSERVE_METRICS_URL || process.env.VLLM_METRICS_URL || process.env.DCGM_METRICS_URL)) {
    collectProductionTelemetry(pool).catch(err => console.error("Initial production telemetry collection failed:", err));
    setInterval(() => collectProductionTelemetry(pool).catch(err => console.error("Production telemetry collection failed:", err)), telemetryInterval);
    console.log(`Production monitoring telemetry collection running every ${telemetryInterval}ms`);
  }

}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error("Failed to start server:", err);
  process.exit(1);
});
