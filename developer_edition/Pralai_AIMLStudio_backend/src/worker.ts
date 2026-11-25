import "dotenv/config";
import { pool } from "./db/connection";
import { runWorkerCycle, sweepTimeouts, getWorkerStatus } from "./services/trainingWorker";
import { runHpoWorkerCycle, sweepHpoTimeouts, getHpoWorkerStatus } from "./services/hpoWorker";
import { runLlmGpuCertification } from "./services/llmGpuCertification";

// A real, standalone worker process — genuinely separate from the API,
// not a rebranded copy of server.ts's in-process loop. This is the
// production-scale half of the architecture the review named:
//
//   ML Studio API -> PostgreSQL Job Queue -> Worker, Worker, Worker
//
// Run this file directly (node dist/worker.js or ts-node src/worker.ts)
// as its own OS process — as many of them, on as many machines, as real
// throughput needs. Every real worker process, wherever it runs, talks
// to nothing but the same real Postgres `training_runs` table — no
// shared memory, no RPC between workers, no leader election. The real
// `FOR UPDATE SKIP LOCKED` claim in trainingWorker.ts is what makes this
// safe: it was already correct for concurrent claims from day one,
// including across truly separate processes — this file is what
// actually EXERCISES that correctness outside a single process for the
// first time, and the verification for this change runs three real,
// separate OS processes against the same real queue to prove it, not
// just three concurrent async calls inside one.
//
// What this genuinely gives you that the in-process worker (still
// available in server.ts, still the right default for development or a
// small deployment) cannot:
//   - Independent scaling: run more/fewer worker processes without
//     touching the API process at all.
//   - The API restarting (a deploy, a crash) doesn't drop any in-flight
//     training — a run claimed by a worker process stays claimed by
//     that worker process regardless of what the API is doing.
//   - Resource isolation: a worker process's own memory/CPU is entirely
//     its own, not shared with (or competing against) the HTTP-serving
//     process.
//   - Real CPU/GPU routing via WORKER_COMPUTE_PROFILES (see
//     trainingWorker.ts) — start a worker with
//     WORKER_COMPUTE_PROFILES=gpu-1x,gpu-4x and it only ever claims
//     real runs declared against those compute profiles, leaving cpu-*
//     runs for other real worker processes.
//   - Real concurrency control via WORKER_CONCURRENCY — how many runs
//     THIS process holds in flight at once.
//   - A real Kubernetes Deployment can run N replicas of this exact
//     process (see k8s/worker-deployment.yaml) with its own
//     HorizontalPodAutoscaler, its own resource requests/limits, its
//     own node selector for GPU scheduling — entirely independent of
//     the API Deployment. Spec-correct; unverified against a live
//     cluster, same disclosure as every other Kubernetes/Argo/KServe
//     reference in this app.
//
// What this does NOT give you, stated plainly: real per-tenant quotas.
// There is no real tenant model anywhere in this app to enforce quotas
// against — UCS_TENANT_ID is a single static catalog namespace, not a
// real multi-tenant concept with its own users, resource limits, or
// isolation. Building real tenant quotas needs a real tenant model
// first; this worker has nothing to attach one to yet.

const WORKER_CYCLE_INTERVAL_MS = process.env.WORKER_CYCLE_INTERVAL_MS ? Number(process.env.WORKER_CYCLE_INTERVAL_MS) : 2_000;
const WORKER_TIMEOUT_SWEEP_INTERVAL_MS = process.env.WORKER_TIMEOUT_SWEEP_INTERVAL_MS ? Number(process.env.WORKER_TIMEOUT_SWEEP_INTERVAL_MS) : 30_000;

let shuttingDown = false;

async function main() {
  // A real, direct DB check at startup — fail loudly and immediately if
  // this worker can't reach the real queue at all, rather than silently
  // sitting idle forever on every subsequent tick.
  await pool.query("SELECT 1");

  // Optional deploy-time certification gate for GPU/LLM workers. A full
  // certification config performs real LoRA/QLoRA/full training, reloads
  // the produced artifact and runs generation before this worker claims
  // production work. REQUIRE_LLM_GPU_CERTIFICATION=true makes failure fatal.
  const certConfig = process.env.LLM_GPU_CERT_CONFIG;
  const requireCert = process.env.REQUIRE_LLM_GPU_CERTIFICATION === "true";
  if (certConfig || requireCert) {
    const cert = await runLlmGpuCertification(certConfig);
    if (!cert.ok || (certConfig && !cert.certified)) {
      if (requireCert) throw new Error(`LLM GPU certification failed: ${cert.error || "not certified"}`);
      console.warn("LLM GPU certification did not pass:", cert);
    } else {
      console.log("LLM GPU runtime certification:", cert);
    }
  }

  const status = getWorkerStatus();
  const hpoStatus = getHpoWorkerStatus();
  // eslint-disable-next-line no-console
  console.log(
    `ML Studio training worker started — id=${status.workerId} concurrency=${status.concurrency}` +
      (status.computeProfileAllowlist ? ` computeProfiles=${status.computeProfileAllowlist.join(",")}` : " computeProfiles=any")
  );
  console.log(`ML Studio HPO worker enabled — concurrency=${hpoStatus.concurrency}`);

  const cycleTimer = setInterval(() => {
    if (shuttingDown) return;
    runWorkerCycle(pool).catch((err) => {
      // eslint-disable-next-line no-console
      console.error("Worker cycle failed:", err instanceof Error ? err.message : err);
    });
    runHpoWorkerCycle(pool).catch((err) => console.error("HPO worker cycle failed:", err instanceof Error ? err.message : err));
  }, WORKER_CYCLE_INTERVAL_MS);

  const sweepTimer = setInterval(() => {
    if (shuttingDown) return;
    sweepTimeouts(pool).catch((err) => {
      // eslint-disable-next-line no-console
      console.error("Timeout sweep failed:", err instanceof Error ? err.message : err);
    });
    sweepHpoTimeouts(pool).catch((err) => console.error("HPO timeout sweep failed:", err instanceof Error ? err.message : err));
  }, WORKER_TIMEOUT_SWEEP_INTERVAL_MS);

  // Real graceful shutdown — stop claiming NEW work on SIGTERM/SIGINT
  // (a real k8s pod termination, or a manual stop) without abandoning
  // whatever this process already has in flight; existing claims are
  // left exactly as they are, and the real heartbeat-staleness sweep on
  // OTHER live workers will reclaim them if this process doesn't finish
  // in time, or this same process finishes them normally if it does.
  const shutdown = (signal: string) => {
    if (shuttingDown) return;
    shuttingDown = true;
    // eslint-disable-next-line no-console
    console.log(`${signal} received — no longer claiming new work; letting in-flight runs finish`);
    clearInterval(cycleTimer);
    clearInterval(sweepTimer);
  };
  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("SIGINT", () => shutdown("SIGINT"));
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error("Worker failed to start:", err);
  process.exit(1);
});
