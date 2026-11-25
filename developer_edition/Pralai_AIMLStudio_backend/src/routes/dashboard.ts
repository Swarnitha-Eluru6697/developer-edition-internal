import { Router, Request, Response } from "express";
import { pool } from "../db/connection";
import { PERM, requirePermission } from "../middleware/permissions";

/**
 * Dashboard aggregates.
 *
 * These exist because the Dashboard needs three things the per-resource routes
 * don't answer: what happened recently, how much training ran over time, and
 * how much compute that training actually consumed. Every one of them is a real
 * query over real rows — none of them synthesize a value when the underlying
 * table is empty, they return an empty series and let the UI say so. A freshly
 * seeded install therefore shows genuinely empty panels rather than a plausible
 * looking demo curve, which is the same fail-closed posture the rest of the
 * platform takes toward data it doesn't have.
 */
export const dashboardRouter = Router();

/** Clamp a `?days=` query param to something a dashboard can sensibly chart. */
function days(req: Request, fallback: number, max = 90): number {
  const raw = Number(req.query.days);
  if (!Number.isFinite(raw)) return fallback;
  return Math.min(Math.max(Math.trunc(raw), 1), max);
}

/**
 * Recent platform activity, straight from the real audit log that
 * `utils/audit.ts` writes on genuinely audited actions (promotion, rollback,
 * and the dataset lifecycle). Empty until something actually happens — it is
 * not backfilled with seed rows, so an entry here always corresponds to a real
 * action somebody took.
 */
dashboardRouter.get("/dashboard/activity", requirePermission(PERM.dashboard), async (req: Request, res: Response) => {
  const limit = Math.min(Math.max(Number(req.query.limit) || 8, 1), 50);
  const result = await pool.query(
    `SELECT actor, action, target, created_at
       FROM audit_log
      ORDER BY created_at DESC
      LIMIT $1`,
    [limit]
  );
  res.json(
    result.rows.map((r) => ({
      actor: r.actor,
      action: r.action,
      target: r.target,
      createdAt: r.created_at,
    }))
  );
});

/**
 * Training runs bucketed per day for the last N days.
 *
 * Returns one entry per day including days with zero runs, so the caller can
 * chart a continuous series without inventing the gaps. `hasData` distinguishes
 * "no runs have ever been recorded" from "a real, measured run of zeros" —
 * those are different facts and the UI renders them differently.
 */
dashboardRouter.get("/dashboard/training-activity", requirePermission(PERM.dashboard), async (req: Request, res: Response) => {
  const n = days(req, 14);
  const result = await pool.query(
    `SELECT to_char(d, 'YYYY-MM-DD') AS day,
            COUNT(t.id)::int AS runs,
            COUNT(*) FILTER (WHERE t.status = 'succeeded')::int AS succeeded,
            COUNT(*) FILTER (WHERE t.status = 'failed')::int    AS failed
       FROM generate_series(
              CURRENT_DATE - ($1::int - 1),
              CURRENT_DATE,
              '1 day'
            ) AS d
       LEFT JOIN training_runs t
              ON t.created_at >= d AND t.created_at < d + INTERVAL '1 day'
      GROUP BY d
      ORDER BY d`,
    [n]
  );
  const series = result.rows.map((r) => ({
    day: String(r.day),
    runs: r.runs,
    succeeded: r.succeeded,
    failed: r.failed,
  }));
  res.json({
    days: n,
    hasData: series.some((p) => p.runs > 0),
    total: series.reduce((a, p) => a + p.runs, 0),
    series,
  });
});

/**
 * Real compute consumed per profile over the last N days.
 *
 * Measured, not estimated: a run's cost is the wall-clock time a worker
 * genuinely held it — `claimed_at` until its last `heartbeat_at` (or now, for a
 * run still in flight). Runs that were never claimed contributed no compute and
 * are counted separately rather than folded in as zero-hour rows.
 *
 * This is worker occupancy, which is the only thing the platform actually
 * observes. It is deliberately NOT presented as billing: there is no cloud cost
 * integration here, and multiplying hours by a made-up rate would be inventing
 * a number nobody measured.
 */
dashboardRouter.get("/dashboard/compute-spend", requirePermission(PERM.dashboard), async (req: Request, res: Response) => {
  const n = days(req, 7);
  const result = await pool.query(
    `SELECT p.id,
            p.label,
            p.kind,
            COALESCE(SUM(
              EXTRACT(EPOCH FROM (COALESCE(t.heartbeat_at, NOW()) - t.claimed_at))
            ) FILTER (WHERE t.claimed_at IS NOT NULL), 0) AS seconds,
            COUNT(t.id) FILTER (WHERE t.claimed_at IS NOT NULL)::int AS runs,
            COUNT(t.id) FILTER (WHERE t.claimed_at IS NULL)::int     AS unclaimed
       FROM compute_pools p
       LEFT JOIN training_runs t
              ON t.compute_profile = p.id
             AND t.created_at >= NOW() - ($1::int * INTERVAL '1 day')
      GROUP BY p.id, p.label, p.kind
      ORDER BY p.id`,
    [n]
  );
  const profiles = result.rows.map((r) => ({
    id: r.id,
    label: r.label,
    kind: r.kind,
    hours: Math.round((Number(r.seconds) / 3600) * 10) / 10,
    runs: r.runs,
    unclaimedRuns: r.unclaimed,
  }));
  res.json({
    days: n,
    measured: "worker-held wall clock (claimed_at → last heartbeat)",
    hasData: profiles.some((p) => p.hours > 0),
    totalHours: Math.round(profiles.reduce((a, p) => a + p.hours, 0) * 10) / 10,
    profiles,
  });
});
