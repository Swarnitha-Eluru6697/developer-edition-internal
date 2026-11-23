import { Router, Request, Response } from "express";
import { pool } from "../db/connection";
import { PERM, requirePermission } from "../middleware/permissions";

export const clustersRouter = Router();

/**
 * Compute pool utilization.
 *
 * `capacity` is declared configuration — how many slots this pool is defined to
 * have. `inUse` and `queuedJobs` are NOT configuration: they are counted live
 * from the same Postgres queue the workers claim from, so what this reports is
 * what is actually running right now. They were previously static seed columns
 * that never changed, which meant this page showed a fixed utilization figure
 * regardless of what the platform was really doing.
 *
 * A training run occupies a slot while a worker holds it (status 'running' with
 * a claim); an HPO trial does the same against the pool its parent search runs
 * on. Nothing else is counted, because nothing else consumes a slot.
 */
clustersRouter.get("/compute-pools", requirePermission(PERM.clusters), async (_req: Request, res: Response) => {
  const result = await pool.query(
    `SELECT p.id, p.label, p.kind, p.capacity,
            (
              SELECT COUNT(*)::int FROM training_runs t
               WHERE t.compute_profile = p.id AND t.status = 'running' AND t.claimed_by IS NOT NULL
            ) + (
              SELECT COUNT(*)::int FROM hpo_trials tr
               WHERE tr.compute_profile = p.id AND tr.status = 'running' AND tr.claimed_by IS NOT NULL
            ) AS in_use,
            (
              SELECT COUNT(*)::int FROM training_runs t
               WHERE t.compute_profile = p.id AND t.status = 'queued'
            ) + (
              SELECT COUNT(*)::int FROM hpo_trials tr
               WHERE tr.compute_profile = p.id AND tr.status = 'queued'
            ) AS queued_jobs
       FROM compute_pools p
      ORDER BY p.id`
  );
  res.json({
    measuredFrom: "live queue state — claimed running runs and trials per compute profile",
    pools: result.rows.map((r) => ({
      id: r.id,
      label: r.label,
      kind: r.kind,
      capacity: r.capacity,
      inUse: r.in_use,
      queuedJobs: r.queued_jobs,
      utilizationPct: r.capacity > 0 ? Math.round((r.in_use / r.capacity) * 100) : 0,
    })),
  });
});

/**
 * GET /api/cluster/telemetry — what the platform can and cannot see about the
 * cluster underneath it.
 *
 * The design for this page assumes a Prometheus scrape of node, pod, DCGM GPU,
 * KEDA and KServe metrics. No such source is configured here, and there is no
 * honest way to synthesize node counts or GPU memory pressure from an
 * application database — so rather than render plausible numbers, this reports
 * per-section availability and exactly what each section would need.
 *
 * Set PROMETHEUS_URL to a reachable Prometheus and these sections become
 * answerable; until then `available: false` is the truthful state.
 */
clustersRouter.get("/cluster/telemetry", requirePermission(PERM.clusters), async (_req: Request, res: Response) => {
  const prometheusUrl = (process.env.PROMETHEUS_URL || "").trim();
  const section = (name: string, requires: string) => ({
    name,
    available: false,
    requires,
  });
  res.json({
    prometheusConfigured: !!prometheusUrl,
    prometheusUrl: prometheusUrl || null,
    // Deliberately not "0 nodes" or "—": the distinction is that these are
    // unmeasured, not measured-as-empty.
    reason: prometheusUrl
      ? "PROMETHEUS_URL is set but this build does not yet query it — these sections stay unavailable rather than reporting unverified values."
      : "No PROMETHEUS_URL is configured, and this platform runs its training locally rather than on Kubernetes, so there is no cluster to scrape.",
    sections: [
      section("Nodes and pods", "kube-state-metrics (kube_node_info, kube_pod_status_phase)"),
      section("GPU utilization and memory", "NVIDIA DCGM exporter (DCGM_FI_DEV_GPU_UTIL, DCGM_FI_DEV_FB_USED)"),
      section("KEDA autoscaling", "KEDA operator metrics (keda_scaler_active, keda_scaled_object_errors)"),
      section("KServe / vLLM serving", "KServe and vLLM exporters (request rate, queue depth, TTFT)"),
    ],
  });
});
