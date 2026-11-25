import { Router, Request, Response } from "express";
import { z } from "zod";
import { randomUUID } from "crypto";
import { pool } from "../db/connection";
import { PERM, requireAnyPermission, requirePermission } from "../middleware/permissions";

export const experimentsRouter = Router();

function toExperimentDto(row: any) {
  return {
    id: row.id,
    name: row.name,
    objectiveMetric: row.objective_metric,
    objectiveDirection: row.objective_direction,
    owner: row.owner,
    team: row.team ?? undefined,
    tags: row.tags ?? undefined,
    description: row.description ?? undefined,
    createdAt: row.created_at,
  };
}

experimentsRouter.get("/experiments", requireAnyPermission(PERM.experiments, PERM.train), async (_req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM experiments ORDER BY created_at DESC");
  res.json(result.rows.map(toExperimentDto));
});

const createExperimentSchema = z.object({
  name: z.string().min(1),
  objectiveMetric: z.string().min(1),
  objectiveDirection: z.enum(["maximize", "minimize"]),
  team: z.string().optional(),
  tags: z.array(z.string()).optional(),
  description: z.string().optional(),
});

experimentsRouter.post("/experiments", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const parsed = createExperimentSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid experiment request", details: parsed.error.flatten() });
  const body = parsed.data;
  const id = randomUUID();
  const result = await pool.query(
    `INSERT INTO experiments (id, name, objective_metric, objective_direction, owner, team, tags, description, created_at)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING *`,
    [id, body.name, body.objectiveMetric, body.objectiveDirection, req.actor, body.team ?? null, body.tags ? JSON.stringify(body.tags) : null, body.description ?? null, new Date().toISOString()]
  );
  res.status(201).json(toExperimentDto(result.rows[0]));
});

// GET /api/experiments/:id — the real "hierarchy" a flat run table was
// missing: every run AND every HPO search that belongs to this
// experiment, plus which single value is currently best against the
// experiment's own declared objective — computed fresh from real rows,
// not cached.
experimentsRouter.get("/experiments/:id", requirePermission(PERM.experiments), async (req: Request, res: Response) => {
  const expResult = await pool.query("SELECT * FROM experiments WHERE id = $1", [req.params.id]);
  if (expResult.rows.length === 0) return res.status(404).json({ error: "experiment not found" });
  const experiment = toExperimentDto(expResult.rows[0]);

  const runsResult = await pool.query("SELECT * FROM training_runs WHERE experiment_id = $1 ORDER BY created_at DESC", [req.params.id]);
  const searchesResult = await pool.query("SELECT * FROM hpo_searches WHERE experiment_id = $1 ORDER BY created_at DESC", [req.params.id]);

  const runs = runsResult.rows.map((r) => ({
    id: r.id,
    modelName: r.model_name,
    baseModelId: r.base_model_id,
    status: r.status,
    metrics: r.metrics ?? undefined,
    createdAt: r.created_at,
  }));

  const objectiveKey = experiment.objectiveMetric;
  const candidateValues: { source: string; label: string; value: number }[] = [];
  for (const r of runs) {
    const v = r.metrics && r.metrics[objectiveKey] !== undefined ? Number(r.metrics[objectiveKey]) : null;
    if (v !== null && !Number.isNaN(v)) candidateValues.push({ source: "run", label: r.modelName, value: v });
  }
  const searches = [];
  for (const s of searchesResult.rows) {
    const trialsResult = await pool.query("SELECT * FROM hpo_trials WHERE search_id = $1 ORDER BY trial_number ASC", [s.id]);
    searches.push({
      id: s.id,
      baseModelId: s.base_model_id,
      datasetName: s.dataset_name,
      searchSpace: s.search_space,
      numTrials: s.num_trials,
      objectiveMetric: s.objective_metric,
      objectiveDirection: s.objective_direction,
      status: s.status,
      bestTrialId: s.best_trial_id,
      createdAt: s.created_at,
      trials: trialsResult.rows.map((t) => ({
        id: t.id, trialNumber: t.trial_number, hyperparams: t.hyperparams, metricValue: t.metric_value,
        dataSource: t.data_source, status: t.status, error: t.error ?? undefined, isBest: t.id === s.best_trial_id,
      })),
    });
    if (s.best_trial_id) {
      const best = trialsResult.rows.find((t: any) => t.id === s.best_trial_id);
      if (best && best.metric_value !== null) candidateValues.push({ source: "hpo", label: `HPO trial #${best.trial_number}`, value: Number(best.metric_value) });
    }
  }

  let bestOverall: { source: string; label: string; value: number } | null = null;
  for (const c of candidateValues) {
    if (!bestOverall) bestOverall = c;
    else if (experiment.objectiveDirection === "maximize" ? c.value > bestOverall.value : c.value < bestOverall.value) bestOverall = c;
  }

  res.json({ ...experiment, runs, hpoSearches: searches, bestOverall });
});
