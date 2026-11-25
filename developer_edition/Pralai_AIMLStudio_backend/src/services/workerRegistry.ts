import { Pool } from "pg";
import os from "os";

// Every worker loop (training and HPO, in the API process or a standalone
// src/worker.ts) writes one row here on each poll cycle. That is what lets the
// fleet view list an IDLE worker — one holding no claimed run touches no queue
// row, so queue state alone can't see it. The row says only what the loop
// itself knows: its id, which compute profiles it will claim, how many slots it
// has and how many are in use. Rows nobody refreshes age out of the view.

export type WorkerRole = "training" | "hpo";

export function poolFor(profiles: string[] | null): "cpu" | "gpu" | "any" {
  if (!profiles?.length) return "any";
  if (profiles.every((p) => p.startsWith("gpu"))) return "gpu";
  if (profiles.every((p) => p.startsWith("cpu"))) return "cpu";
  return "any";
}

export async function reportWorkerHeartbeat(
  pool: Pool,
  role: WorkerRole,
  s: { workerId: string; concurrency: number; inFlight: number; computeProfileAllowlist: string[] | null }
): Promise<void> {
  await pool.query(
    `INSERT INTO worker_heartbeats (worker_id, role, hostname, pid, pool, compute_profiles, concurrency, in_flight, started_at, last_seen)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW(),NOW())
     ON CONFLICT (worker_id, role) DO UPDATE
       SET pool=EXCLUDED.pool, compute_profiles=EXCLUDED.compute_profiles, concurrency=EXCLUDED.concurrency,
           in_flight=EXCLUDED.in_flight, last_seen=NOW()`,
    [s.workerId, role, os.hostname(), process.pid, poolFor(s.computeProfileAllowlist), s.computeProfileAllowlist, s.concurrency, s.inFlight]
  ).catch(() => {}); // a missed beat only makes the row look older; never break the claim loop over it
}
