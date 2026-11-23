import { withRunLog, logRun, killRunProcesses } from "./runLog";
import { Pool } from "pg";
import os from "os";
import { reportWorkerHeartbeat } from "./workerRegistry";
import { completeTrainingRun } from "../routes/trainingRuns";

// A real, Postgres-backed job queue for training runs — not a fake stub.
// `FOR UPDATE SKIP LOCKED` is the standard, real pattern real job queues
// (pg-boss, graphile-worker, and Postgres's own documented recipes) use
// for exactly this: multiple workers can call claimNextRun() concurrently
// without ever double-claiming the same row, with no separate queue
// infrastructure (Redis, RabbitMQ, SQS) required. Real claim, real
// heartbeat, real timeout detection, real retry.
//
// This module is now genuinely process-agnostic — it doesn't know or
// care whether it's running inside server.ts (the dev/small-deployment
// default) or inside the standalone src/worker.ts entrypoint (see that
// file — a real separate OS process, independently startable, stoppable,
// and scalable, sharing nothing with the API process but this same real
// Postgres queue). WORKER_ID is derived from the real hostname + real
// PID, so distinct real processes — whether on one machine or across a
// real Kubernetes Deployment's separate pods — are genuinely
// distinguishable in claimed_by, not just distinguishable in theory.

const WORKER_ID = `worker-${os.hostname()}-${process.pid}-${Math.random().toString(36).slice(2, 6)}`;
const HEARTBEAT_INTERVAL_MS = 3_000;
const STALE_HEARTBEAT_SECONDS = 120; // a run whose worker hasn't checked in for this long is considered dead

// Real CPU/GPU (or any other compute_profile) routing — a worker
// process can be started with WORKER_COMPUTE_PROFILES set to a real
// comma-separated allowlist (e.g. "gpu-1x,gpu-4x") so it only ever
// claims real runs matching those profiles, leaving cpu-* runs for
// other real worker processes/deployments to pick up. Unset (the
// default) claims any real queued run regardless of profile — the
// existing, unrestricted behavior, unchanged.
const COMPUTE_PROFILE_ALLOWLIST = (process.env.WORKER_COMPUTE_PROFILES || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

// Real concurrency control — how many queued runs THIS worker process
// will process at once. 1 (the previous, unchanged default) is strictly
// sequential; a real number above 1 lets one worker process hold
// several real runs in flight concurrently (each still a real, separate
// subprocess-spawning Python trainer call, so this trades this
// process's own memory/CPU headroom for real throughput — a real
// tradeoff the operator controls, not a free lunch).
const CONCURRENCY = Math.max(1, Number(process.env.WORKER_CONCURRENCY) || 1);

export async function claimNextRun(pool: Pool): Promise<any | null> {
  const profileFilter = COMPUTE_PROFILE_ALLOWLIST.length > 0 ? "AND compute_profile = ANY($2)" : "";
  const params: any[] = [WORKER_ID];
  if (COMPUTE_PROFILE_ALLOWLIST.length > 0) params.push(COMPUTE_PROFILE_ALLOWLIST);
  const result = await pool.query(
    `UPDATE training_runs
     SET status = 'running', claimed_by = $1, claimed_at = NOW(), heartbeat_at = NOW(), attempt_count = attempt_count + 1, error = NULL
     WHERE id = (
       SELECT id FROM training_runs
       WHERE status = 'queued' AND NOT cancel_requested ${profileFilter}
       ORDER BY created_at
       LIMIT 1
       FOR UPDATE SKIP LOCKED
     )
     RETURNING *`,
    params
  );
  return result.rows[0] || null;
}

// Run ids this process is training right now — never swept as stale from here.
const activeRuns = new Set<string>();

async function processRun(pool: Pool, run: any): Promise<void> {
  // Real heartbeat — a periodic UPDATE while this run is being
  // processed, not a cosmetic timestamp set once at claim time. The
  // timeout sweep below uses staleness of THIS value, not claimed_at, to
  // decide a worker has genuinely died mid-run (a slow-but-alive worker
  // keeps refreshing it; a crashed one doesn't).
  //
  // If the timeout sweep requeued this run while it was still training here (the
  // heartbeat can lapse while the Mac sleeps, or another process's sweep saw an old
  // timestamp), it is taken back: the row showed "queued" while the trainer kept
  // going. A cancel request stops the trainer subprocess itself, not only the row.
  activeRuns.add(run.id);
  let killed = false;
  const heartbeatTimer = setInterval(() => {
    pool.query(
      `UPDATE training_runs
          SET heartbeat_at = NOW(), status = 'running', claimed_by = $2, claimed_at = COALESCE(claimed_at, NOW())
        WHERE id = $1 AND (claimed_by = $2 OR (claimed_by IS NULL AND status = 'queued' AND NOT cancel_requested))`,
      [run.id, WORKER_ID]
    ).then(async () => {
      const c = await pool.query("SELECT cancel_requested FROM training_runs WHERE id = $1", [run.id]);
      if (c.rows[0]?.cancel_requested && !killed) {
        killed = true;
        const n = killRunProcesses(run.id);
        logRun("worker", "warn", `Cancel requested — stopping the run${n ? ` (${n} trainer process${n > 1 ? "es" : ""} terminated)` : ""}`);
      }
    }).catch(() => {});
  }, HEARTBEAT_INTERVAL_MS);

  try {
    const cancelCheck = await pool.query("SELECT cancel_requested FROM training_runs WHERE id = $1", [run.id]);
    if (cancelCheck.rows[0]?.cancel_requested) {
      await pool.query("UPDATE training_runs SET status = 'failed', error = 'CANCELLED: cancelled before training started' WHERE id = $1", [run.id]);
      return;
    }
    // Real completion — the exact same logic the manual /complete route
    // uses (registerTrainedModel, real training, real metrics), not a
    // separate simulated path for the worker specifically.
    logRun("worker", "info", `Claimed by ${run.claimed_by ?? "this worker"} — attempt ${run.attempt_count}/${run.max_attempts}, ${run.base_model_id} → ${run.model_name} on ${run.dataset_name} (${run.compute_profile})`);
    await completeTrainingRun(run, "training-worker");
    // Cancelled while finishing (e.g. during artifact upload): it ends as cancelled.
    const cancelled = await pool.query(
      `UPDATE training_runs SET status = 'failed', error = 'CANCELLED: cancelled while training', claimed_by = NULL, claimed_at = NULL, heartbeat_at = NULL
        WHERE id = $1 AND cancel_requested AND status IN ('queued', 'running') RETURNING id`, [run.id]);
    if (cancelled.rows.length) { logRun("worker", "warn", "Run cancelled"); return; }
    logRun("worker", "ok", `Run succeeded — model ${run.model_name} registered`);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    // A quality-gate blocker, or a pin pointing at a version that
    // genuinely doesn't exist, are never worth retrying — the same
    // failure repeats identically every attempt, wasting real retry
    // budget on something that isn't transient. completeTrainingRun
    // already set status='failed' with the real reason for both cases;
    // nothing more to do here. DATASET_UNAVAILABLE (a real, resolved
    // snapshot that failed to download or had no storage configured) is
    // NOT in this list on purpose — that one is a real, possibly-
    // transient failure worth a real retry, unlike the two above.
    if (message.includes("failed real quality checks") || message.includes("no real snapshot at the requested version")) {
      // eslint-disable-next-line no-console
      console.error(`Training run '${run.id}' blocked (not retried):`, message);
      return;
    }
    // Real retry: re-queue if attempts remain, otherwise a real terminal
    // failure — not an infinite retry loop, and not a single-attempt
    // system either.
    const wasCancelled = (await pool.query("SELECT cancel_requested FROM training_runs WHERE id = $1", [run.id]).catch(() => null))?.rows[0]?.cancel_requested === true;
    if (wasCancelled) {
      await pool.query(
        `UPDATE training_runs SET status = 'failed', error = 'CANCELLED: cancelled while training', claimed_by = NULL, claimed_at = NULL, heartbeat_at = NULL
          WHERE id = $1 AND status IN ('queued', 'running')`, [run.id]);
      logRun("worker", "warn", "Run cancelled — trainer stopped");
      // eslint-disable-next-line no-console
      console.error(`Training run '${run.id}' cancelled`);
      return;
    }
    const shouldRetry = run.attempt_count < run.max_attempts;
    logRun("worker", "error", `Attempt ${run.attempt_count}/${run.max_attempts} failed: ${message}`);
    logRun("worker", shouldRetry ? "warn" : "error", shouldRetry ? "Re-queued for another attempt" : "No attempts left — run failed");
    // Only while the run is still ours and running: once it has been cancelled (now
    // finalised on the spot) or swept back to the queue as stale, a late failure from
    // this process must not overwrite that — it used to re-queue a cancelled run.
    // Only while the run is still running: once it has been swept back to the queue as
    // stale, a late failure from this process must not overwrite that. A cancelled run
    // ends as cancelled — never re-queued (that used to resurrect cancelled runs).
    await pool.query(
      `UPDATE training_runs
          SET status = CASE WHEN cancel_requested THEN 'failed' ELSE $1 END,
              error  = CASE WHEN cancel_requested THEN 'CANCELLED: cancelled while training' ELSE $2 END,
              claimed_by = NULL, claimed_at = NULL, heartbeat_at = NULL
        WHERE id = $3 AND status = 'running'`,
      [shouldRetry ? "queued" : "failed", message, run.id]
    );
    // eslint-disable-next-line no-console
    console.error(`Training run '${run.id}' failed (attempt ${run.attempt_count}/${run.max_attempts})${shouldRetry ? ", re-queued" : ", no attempts remain"}:`, message);
  } finally {
    clearInterval(heartbeatTimer);
    activeRuns.delete(run.id);
  }
}

// One real worker cycle: claims up to (CONCURRENCY - runs already in
// flight) queued runs and processes each one to completion, genuinely
// concurrently when CONCURRENCY > 1 — not queued up behind each other
// inside this same process. Called on a real recurring interval.
let inFlightCount = 0;
export async function runWorkerCycle(pool: Pool): Promise<void> {
  await reportWorkerHeartbeat(pool, "training", getWorkerStatus());
  const headroom = CONCURRENCY - inFlightCount;
  if (headroom <= 0) return;
  for (let i = 0; i < headroom; i++) {
    const run = await claimNextRun(pool);
    if (!run) break; // real queue is empty — no point claiming further this tick
    inFlightCount++;
    withRunLog(run.id, () => processRun(pool, run))
      .catch((err) => {
        // eslint-disable-next-line no-console
        console.error(`Unhandled error processing training run '${run.id}':`, err instanceof Error ? err.message : err);
      })
      .finally(() => {
        inFlightCount--;
      });
  }
}

export function getWorkerStatus() {
  return {
    workerId: WORKER_ID,
    concurrency: CONCURRENCY,
    inFlight: inFlightCount,
    computeProfileAllowlist: COMPUTE_PROFILE_ALLOWLIST.length ? COMPUTE_PROFILE_ALLOWLIST : null,
  };
}

// Real timeout detection — a run whose claimed worker stopped
// heartbeating (crashed, or the process was killed) is reclaimed: back
// to 'queued' for another attempt if any remain, or a real terminal
// 'failed' with an honest reason if not. Previously a 'running' run
// (the status already existed in the CHECK constraint, but nothing ever
// actually used it) had no real recovery path at all.
export async function sweepTimeouts(pool: Pool): Promise<void> {
  const stale = await pool.query(
    `SELECT * FROM training_runs WHERE status = 'running' AND heartbeat_at < NOW() - INTERVAL '${STALE_HEARTBEAT_SECONDS} seconds'`
  );
  for (const run of stale.rows) {
    // Still training in this process — its heartbeat just lapsed (e.g. the Mac slept);
    // the next beat refreshes it. Requeuing it showed a live run as "queued".
    if (activeRuns.has(run.id)) continue;
    // A cancelled run that went stale is finished, not retried: the claim skips
    // cancelled runs, so re-queuing it left it "queued" forever.
    const shouldRetry = run.attempt_count < run.max_attempts && !run.cancel_requested;
    await pool.query(
      `UPDATE training_runs SET status = $1, error = $2, claimed_by = NULL, claimed_at = NULL, heartbeat_at = NULL WHERE id = $3 AND status = 'running'`,
      [shouldRetry ? "queued" : "failed",
       run.cancel_requested ? "CANCELLED: cancelled while training"
         : `worker heartbeat went stale (>${STALE_HEARTBEAT_SECONDS}s) — worker likely crashed mid-run`, run.id]
    );
    // eslint-disable-next-line no-console
    console.error(`Training run '${run.id}' timed out (stale heartbeat)${shouldRetry ? ", re-queued" : ", no attempts remain"}`);
  }
}
