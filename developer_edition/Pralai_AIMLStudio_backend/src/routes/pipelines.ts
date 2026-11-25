import { Router, Request, Response } from "express";
import { z } from "zod";
import { randomUUID } from "crypto";
import { pool } from "../db/connection";
import { PERM, requireAnyPermission, requirePermission } from "../middleware/permissions";
import { logAudit } from "../utils/audit";
import { getWorkflowEngine } from "../execution/workflowEngine";

export const pipelinesRouter = Router();

// ---- Pipeline Definitions: immutable, versioned — what a pipeline IS,
// separate from any one execution of it. ----

function definitionDto(r: any) {
  return { id: r.id, name: r.name, version: r.version, stages: r.stages, computeProfile: r.compute_profile, createdBy: r.created_by, createdAt: r.created_at };
}

pipelinesRouter.get("/pipeline-definitions", requirePermission(PERM.pipelines), async (_req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM pipeline_definitions ORDER BY name, version DESC");
  res.json(result.rows.map(definitionDto));
});

pipelinesRouter.get("/pipeline-definitions/:name", requirePermission(PERM.pipelines), async (req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM pipeline_definitions WHERE name = $1 ORDER BY version DESC", [req.params.name]);
  if (result.rows.length === 0) return res.status(404).json({ error: "no definition found with this name" });
  res.json(result.rows.map(definitionDto));
});

// A real stage now declares what it actually DOES — not just a display
// title. See stageParamsSchemas below for exactly what each real type
// needs; 'generic' (the default) is an honest no-op placeholder for a
// stage nothing real has been wired up for yet, not a silent skip.
const stageTypeSchema = z.enum(["dataset-validation", "preprocessing", "training", "hpo", "evaluation", "register", "deploy", "generic"]);
const stageSchema = z.object({
  title: z.string().min(1),
  type: stageTypeSchema.default("generic"),
  params: z.record(z.any()).optional(),
});

const createDefinitionSchema = z.object({
  name: z.string().min(1),
  stages: z.array(stageSchema).min(1),
  computeProfile: z.string().min(1),
});

// POST /api/pipeline-definitions — creates a real new version. A
// definition is genuinely immutable: calling this again with the same
// name never edits the existing row, it inserts a new one with
// version = MAX(existing version) + 1, real optimistic versioning
// matching the same real transaction pattern model_versions uses.
pipelinesRouter.post("/pipeline-definitions", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const parsed = createDefinitionSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid pipeline definition", details: parsed.error.flatten() });

  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [parsed.data.name]);
    const existing = await client.query("SELECT COALESCE(MAX(version), 0) AS max_v FROM pipeline_definitions WHERE name = $1", [parsed.data.name]);
    const nextVersion = Number(existing.rows[0].max_v) + 1;
    const id = randomUUID();
    const inserted = await client.query(
      `INSERT INTO pipeline_definitions (id, name, version, stages, compute_profile, created_by) VALUES ($1, $2, $3, $4, $5, $6) RETURNING *`,
      [id, parsed.data.name, nextVersion, JSON.stringify(parsed.data.stages), parsed.data.computeProfile, req.actor]
    );
    await client.query("COMMIT");
    await logAudit(req.actor, "created pipeline definition", `${parsed.data.name} v${nextVersion}`);
    res.status(201).json(definitionDto(inserted.rows[0]));
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }
});

// POST /api/pipeline-definitions/:id/run — a real new execution of a
// specific, immutable definition version. Compiles the definition's real
// stages into a real new pipeline run — local executor (real wall-clock
// stage progression) or real Argo Workflow creation, depending on what's
// configured. Previously there was no "run from definition" concept at
// all — every pipeline WAS its own one-off row with no reusable
// definition behind it.
pipelinesRouter.post("/pipeline-definitions/:id/run", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const def = await pool.query("SELECT * FROM pipeline_definitions WHERE id = $1", [req.params.id]);
  if (def.rows.length === 0) return res.status(404).json({ error: "pipeline definition not found" });
  const definition = def.rows[0];

  const pipelineId = `pl-${randomUUID().slice(0, 8)}`;
  const engine = getWorkflowEngine(pool);
  await engine.submitNewWorkflow(pipelineId, `${definition.name} (v${definition.version})`, definition.stages, definition.compute_profile, req.actor);
  await pool.query("UPDATE pipelines SET definition_id = $1, definition_version = $2 WHERE id = $3", [definition.id, definition.version, pipelineId]);

  await logAudit(req.actor, "ran pipeline definition", `${definition.name} v${definition.version} -> ${pipelineId}`);
  res.status(201).json({ pipelineId, definitionName: definition.name, definitionVersion: definition.version, executor: engine.kind });
});


/**
 * A training run projected as a pipeline.
 *
 * "One Argo Workflow per training request" is the model this page is built on,
 * but only `POST /pipeline-definitions/:id/run` ever inserted into `pipelines` —
 * a run launched from the Train wizard produced a `training_runs` row and
 * nothing else, so the page stayed empty no matter how many models you trained.
 *
 * Rather than duplicate state into a second table (two rows that can disagree
 * about the same run), the run IS the workflow and its stages are DERIVED from
 * facts already recorded on it:
 *
 *   validate dataset  the snapshot it resolved and pinned
 *   train             claimed_by / claimed_at / heartbeat_at
 *   evaluate          whether metrics were produced
 *   register          whether a model_versions row exists for it
 *
 * Nothing here is invented: a stage is "pending" precisely when the fact that
 * would make it done has not been recorded yet.
 */
export function runStages(r: any, hasVersion: boolean) {
  const failed = r.status === "failed";
  const done = r.status === "succeeded";
  const started = !!r.claimed_at;
  const st = (ok: boolean, running: boolean, failedHere: boolean) =>
    failedHere ? "failed" : ok ? "ok" : running ? "running" : "pending";

  // A failure is attributed to the furthest stage that had actually begun —
  // the error text says which, and guessing beyond that would be fiction.
  const err: string = r.error || "";
  const failedAtValidate = failed && /dataset|snapshot|quality|column/i.test(err);
  const failedAtTrain = failed && !failedAtValidate;

  return [
    {
      order: 1, title: "Validate dataset",
      state: st(!!r.dataset_snapshot_version || started || done, false, failedAtValidate),
      sub: r.dataset_snapshot_version ? `${r.dataset_name} v${r.dataset_snapshot_version}` : r.dataset_name,
    },
    {
      order: 2, title: "Train",
      state: st(done, started && !done && !failed, failedAtTrain),
      sub: r.claimed_by ? `${r.compute_profile} · ${r.claimed_by}` : r.compute_profile,
    },
    {
      order: 3, title: "Evaluate",
      state: st(done && !!r.metrics, false, false),
      sub: r.metrics ? Object.entries(r.metrics).filter(([k]) => k !== "realDataset").map(([k, v]) => `${k} ${v}`).join(" · ") : "not reached",
    },
    {
      order: 4, title: "Register",
      state: st(hasVersion, false, false),
      sub: hasVersion ? `${r.model_name} registered` : "not reached",
    },
  ];
}

export function runAsPipeline(r: any, hasVersion: boolean) {
  const bits = [r.base_model_id, r.compute_profile, r.dataset_name].filter(Boolean);
  return {
    id: r.id,
    title: `train-${r.model_name}`,
    status: r.status === "queued" ? "queued" : r.status === "running" ? "running" : r.status,
    computeProfile: r.compute_profile,
    meta: bits.join(" · "),
    startedAt: r.claimed_at || r.created_at,
    definitionId: null,
    definitionVersion: null,
    kind: "training-run" as const,
    stages: runStages(r, hasVersion),
  };
}

/** What the list's "Current stage" column shows: the stage actually in
 *  progress, else the furthest one that finished, else the first pending one.
 *  Derived from the same stage projection the detail view renders, so the row
 *  and the DAG can't claim different things. */
function currentStageLabel(stages: { title: string; state: string; sub: string }[], computeProfile?: string): string {
  const running = stages.find((s) => s.state === "running");
  if (running) return `${running.title} · in progress`;
  const failed = stages.find((s) => s.state === "failed");
  if (failed) {
    // A stage's `sub` describes what it was doing, not that it stopped. Without
    // saying so, a failed row read "Train · cpu-small" — indistinguishable from
    // a healthy one at a glance.
    // The compute profile is already on the row; repeating it as the failure
    // detail adds nothing.
    if (!failed.sub || failed.sub === computeProfile) return `${failed.title} · failed`;
    return /fail/i.test(failed.sub) ? `${failed.title} · ${failed.sub}` : `${failed.title} · failed (${failed.sub})`;
  }
  const lastOk = [...stages].reverse().find((s) => s.state === "ok");
  if (lastOk) return `${lastOk.title} · ${lastOk.sub}`;
  return stages.length ? `${stages[0].title} · pending` : "—";
}

pipelinesRouter.get("/pipelines", requirePermission(PERM.pipelines), async (_req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM pipelines ORDER BY started_at DESC");
  const allStages = await pool.query("SELECT * FROM pipeline_stages ORDER BY pipeline_id, stage_order");
  const runs = await pool.query(
    `SELECT t.*, EXISTS (SELECT 1 FROM model_versions v WHERE v.training_run_id = t.id) AS has_version
       FROM training_runs t ORDER BY t.created_at DESC`
  );
  const merged = [
    ...result.rows.map((r) => ({
      id: r.id, title: r.title, status: r.status, computeProfile: r.compute_profile,
      meta: r.meta, startedAt: r.started_at,
      definitionId: r.definition_id, definitionVersion: r.definition_version,
      kind: "definition" as const,
      currentStage: currentStageLabel(
        allStages.rows
          .filter((s) => s.pipeline_id === r.id)
          .map((s) => ({ title: s.title, state: s.state, sub: s.sub })),
        r.compute_profile
      ),
    })),
    ...runs.rows.map((r) => {
      // The full stage list isn't sent (the detail route derives it again), but
      // the one line the list column shows is computed from it here rather than
      // guessed from `status` — a succeeded run and a registered run look the
      // same in `status` and are different stages.
      const { stages, ...rest } = runAsPipeline(r, r.has_version);
      return { ...rest, currentStage: currentStageLabel(stages, r.compute_profile) };
    }),
  ].sort((a, b) => new Date(b.startedAt).getTime() - new Date(a.startedAt).getTime());
  res.json(merged);
});

pipelinesRouter.get("/pipelines/:id", requireAnyPermission(PERM.pipelines, PERM.train, PERM.modelOverview), async (req: Request, res: Response) => {
  const pipeline = await pool.query("SELECT * FROM pipelines WHERE id = $1", [req.params.id]);
  if (pipeline.rows.length === 0) {
    // Not a definition-run — try a training run, which is also a workflow.
    const run = await pool.query(
      `SELECT t.*, EXISTS (SELECT 1 FROM model_versions v WHERE v.training_run_id = t.id) AS has_version
         FROM training_runs t WHERE t.id = $1`,
      [req.params.id]
    );
    if (run.rows.length === 0) return res.status(404).json({ error: "pipeline not found" });
    const r = run.rows[0];
    const projected = runAsPipeline(r, r.has_version);
    // Log lines are the run's own recorded timeline — claim, completion, error.
    // No synthetic Argo chatter: if it wasn't recorded, it isn't shown.
    // `atMs` carries full precision alongside the HH:MM:SS display string.
    // Real runs here finish in a couple of seconds, so second-resolution
    // timestamps collapse every line to the same value and the elapsed column
    // reads "+0s" throughout — true but useless. The caller renders offsets
    // from atMs instead.
    const logs: { t: string; atMs: number | null; src: string; level: string; msg: string }[] = [];
    const at = (d: any) => (d ? new Date(d).toISOString().slice(11, 19) : "--:--:--");
    const ms = (d: any) => (d ? new Date(d).getTime() : null);
    logs.push({ t: at(r.created_at), atMs: ms(r.created_at), src: "training-service", level: "info", msg: `Run queued — ${r.base_model_id} on ${r.dataset_name} (${r.compute_profile})` });
    if (r.claimed_at) logs.push({ t: at(r.claimed_at), atMs: ms(r.claimed_at), src: "worker", level: "info", msg: `Claimed by ${r.claimed_by} — attempt ${r.attempt_count}` });
    if (r.dataset_snapshot_version) logs.push({ t: at(r.claimed_at), atMs: ms(r.claimed_at), src: "dataset", level: "ok", msg: `Pinned ${r.dataset_name} v${r.dataset_snapshot_version}${r.dataset_checksum ? ` · checksum ${String(r.dataset_checksum).slice(0, 12)}…` : ""}` });
    if (r.status === "succeeded" && r.metrics) {
      logs.push({ t: at(r.heartbeat_at || r.created_at), atMs: ms(r.heartbeat_at || r.created_at), src: "trainer", level: "ok", msg: `Training complete — ${Object.entries(r.metrics).filter(([k]) => k !== "realDataset").map(([k, v]) => `${k} ${v}`).join(", ")}` });
    }
    if (r.has_version) logs.push({ t: at(r.heartbeat_at || r.created_at), atMs: ms(r.heartbeat_at || r.created_at), src: "catalog", level: "ok", msg: `Registered ${r.model_name}` });
    if (r.error) logs.push({ t: at(r.heartbeat_at || r.created_at), atMs: ms(r.heartbeat_at || r.created_at), src: "trainer", level: "error", msg: r.error });
    // The live log the run wrote while it worked (services/runLog.ts): trainer progress,
    // per-step loss, metrics, artifact upload, catalog registration. Merged in time order.
    const live = await pool.query(
      "SELECT at, src, level, msg, data FROM training_run_logs WHERE run_id = $1 ORDER BY seq", [r.id]
    ).catch(() => ({ rows: [] as any[] }));
    // With a live log, the reconstructed claim / pin / complete / registered lines only
    // repeat it in less detail: keep "queued" (written before the worker starts).
    if (live.rows.length) logs.splice(1);
    for (const l of live.rows) {
      logs.push({ t: at(l.at), atMs: ms(l.at), src: l.src, level: l.level, msg: l.msg, ...(l.data ? { data: l.data } : {}) } as any);
    }
    logs.sort((a, b) => (a.atMs ?? 0) - (b.atMs ?? 0));
    return res.json({ ...projected, logs, liveLogLines: live.rows.length });
  }
  const stages = await pool.query("SELECT * FROM pipeline_stages WHERE pipeline_id = $1 ORDER BY stage_order", [req.params.id]);
  const logs = await pool.query("SELECT * FROM pipeline_logs WHERE pipeline_id = $1 ORDER BY seq", [req.params.id]);
  const p = pipeline.rows[0];
  // The training run this pipeline produced (written by its training/HPO stage),
  // so the run view can link to it in Experiments. Null until training has run.
  const trained = [...stages.rows].reverse().find((s) => s.stage_output?.trainingRunId)?.stage_output;
  res.json({
    id: p.id,
    title: p.title,
    status: p.status,
    computeProfile: p.compute_profile,
    meta: p.meta,
    startedAt: p.started_at,
    definitionId: p.definition_id,
    definitionVersion: p.definition_version,
    kind: "definition" as const,
    trainingRunId: trained?.trainingRunId ?? null,
    modelName: trained?.modelName ?? null,
    stages: stages.rows.map((s) => ({ order: s.stage_order, title: s.title, state: s.state, sub: s.sub })),
    logs: logs.rows.map((l) => ({ t: l.t, src: l.src, level: l.level, msg: l.msg })),
  });
});

// POST /api/pipelines/:id/restart — only valid on a failed pipeline, same
// rule as the mockup. Resubmits from the first stage, resets the rest to
// pending, and APPENDS a log line documenting the restart rather than
// wiping the original failure log — that log is the reason to restart.
pipelinesRouter.post("/pipelines/:id/restart", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const pipeline = await pool.query("SELECT * FROM pipelines WHERE id = $1", [req.params.id]);
  if (pipeline.rows.length === 0) {
    // A training run is also a workflow (see runAsPipeline above), so its id
    // reaches this route too — and used to 404 here, because this only ever
    // looked at the `pipelines` table. Restarting one means putting it back on
    // the queue for a worker to claim, which is the real equivalent of Argo
    // resubmitting the workflow.
    const run = await pool.query("SELECT * FROM training_runs WHERE id = $1", [req.params.id]);
    if (run.rows.length === 0) return res.status(404).json({ error: "pipeline not found" });
    const r = run.rows[0];
    if (r.status !== "failed") {
      return res.status(409).json({ error: "only a failed run can be restarted" });
    }
    // attempt_count is deliberately NOT reset: the worker increments it on each
    // claim and gives up at max_attempts, so zeroing it here would turn a
    // permanently-failing run into an infinite retry loop.
    const requeued = await pool.query(
      `UPDATE training_runs
          SET status = 'queued', error = NULL, claimed_by = NULL, claimed_at = NULL,
              heartbeat_at = NULL, cancel_requested = FALSE
        WHERE id = $1 AND status = 'failed'
        RETURNING attempt_count, max_attempts`,
      [req.params.id]
    );
    if (requeued.rows.length === 0) {
      return res.status(409).json({ error: "run changed state — reload and try again" });
    }
    const { attempt_count, max_attempts } = requeued.rows[0];
    if (attempt_count >= max_attempts) {
      // Re-queued anyway (an operator asked), but say plainly that the worker's
      // own retry budget is spent, so this will be refused rather than run.
      await logAudit(req.actor, "restarted training run (retry budget exhausted)", req.params.id);
      return res.json({
        ok: true,
        requeued: true,
        warning: `this run has already used ${attempt_count} of ${max_attempts} attempts — the worker will refuse it unless max_attempts is raised`,
      });
    }
    await logAudit(req.actor, "restarted training run", req.params.id);
    return res.json({ ok: true, requeued: true, attempt: attempt_count + 1, maxAttempts: max_attempts });
  }
  if (pipeline.rows[0].status !== "failed") {
    return res.status(409).json({ error: "only a failed pipeline can be restarted" });
  }

  const engine = getWorkflowEngine(pool);
  await engine.restartPipeline(req.params.id, req.actor);

  await logAudit(req.actor, "restarted pipeline", req.params.id);
  // Not cataloged in UCS: the provided admin.py only models Catalog /
  // Model / ModelVersion / Role / Lineage-namespace / Quality-check /
  // Alert-rule entities — no generic "pipeline" or "workflow" object type.
  // A pipeline restart still gets recorded in ML Studio's own audit log
  // above; it just isn't a UCS catalog operation given what's defined.
  res.json({ ok: true, executor: engine.kind });
});
