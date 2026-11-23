import { Router, Request, Response } from "express";
import { pool } from "../db/connection";
import { getLineageGraph, getLineageRuns, getLineageImpact, getObjectProfile, UcsError } from "../ucs/client";
import { modelFqn, ucsIdentityFromPrincipal } from "../ucs/config";
import { syncBaseModelsFromUcs } from "../services/baseModelSync";
import { PERM, requireAnyPermission, requirePermission } from "../middleware/permissions";

export const modelsRouter = Router();

// POST /api/base-models/sync-from-ucs — pulls base_models FROM UCS,
// replacing this table's role as source-of-truth-by-default with "read
// cache of what UCS holds." See baseModelSync.ts's module comment for
// why this is UNVERIFIED (needs a live Gravitino, same as the write side
// in scripts/bootstrapUcs.ts) — this route exists so that verification
// can happen against a real deployment without a code change, not
// because it's been exercised here.
modelsRouter.post("/base-models/sync-from-ucs", requirePermission(PERM.baseModelsSync), async (req: Request, res: Response) => {
  const result = await syncBaseModelsFromUcs(ucsIdentityFromPrincipal(req.principal));
  if (!result.attempted || result.reason) {
    return res.status(result.synced > 0 ? 200 : 502).json(result);
  }
  res.json(result);
});

// GET /api/base-models — the library (classical ML + LLM base models)
modelsRouter.get("/base-models", requireAnyPermission(PERM.modelCatalog, PERM.train, PERM.endpoints, PERM.experiments, PERM.pipelines), async (req: Request, res: Response) => {
  const domain = req.query.domain as string | undefined;
  const result =
    domain === "ml" || domain === "llm"
      ? await pool.query("SELECT * FROM base_models WHERE domain = $1 ORDER BY sort_order, id", [domain])
      : await pool.query("SELECT * FROM base_models ORDER BY sort_order, id");
  res.json(result.rows);
});

/**
 * GET /api/algorithms — the hyperparameter surface of every trainer, and
 * GET /api/algorithms/:key for one. The Train wizard renders its fixed-
 * hyperparameter fields from this, and POST /api/training-runs validates
 * against the same rows, so what the form offers and what the server accepts
 * cannot drift apart.
 */
modelsRouter.get("/algorithms", requireAnyPermission(PERM.modelCatalog, PERM.train), async (_req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM algorithm_specs ORDER BY algorithm_key");
  res.json(result.rows.map(algorithmDto));
});

modelsRouter.get("/algorithms/:key", requireAnyPermission(PERM.modelCatalog, PERM.train), async (req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM algorithm_specs WHERE algorithm_key = $1", [req.params.key]);
  if (result.rows.length === 0) return res.status(404).json({ error: "algorithm not found" });
  res.json(algorithmDto(result.rows[0]));
});

function algorithmDto(row: any) {
  return {
    algorithmKey: row.algorithm_key,
    label: row.label,
    appliesToText: row.applies_to_text ?? null,
    hasAppliesTo: row.has_applies_to,
    defaultAppliesTo: row.default_applies_to ?? null,
    appliesToTooltip: row.applies_to_tooltip ?? null,
    appliesToOptions: row.applies_to_options ?? null,
    fields: row.fields,
  };
}

modelsRouter.get("/base-models/:id", requireAnyPermission(PERM.modelCatalog, PERM.train, PERM.modelOverview), async (req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM base_models WHERE id = $1", [req.params.id]);
  if (result.rows.length === 0) return res.status(404).json({ error: "base model not found" });
  res.json(result.rows[0]);
});

// Real architecture-fidelity check — the catalog's own declared
// framework (base_models.framework, e.g. "PyTorch") never gets
// corrected by what a version was ACTUALLY trained with
// (model_versions.actual_framework). A mismatch here is real and
// disclosed as data, not just a source comment — true whenever a
// deep-learning base model was really trained with the honest
// the real native trainer selected at execution time. Current PyTorch
// catalog models now record native PyTorch execution; unsupported
// architecture/data contracts fail closed. NULL actual_framework means
// "unknown," not
// "matches" — never silently reported as fidelity-confirmed.
function architectureFidelity(declaredFramework: string, actualFramework: string | null) {
  if (!actualFramework) return { checked: false, mismatch: false };
  const normalize = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, "");
  return { checked: true, mismatch: normalize(declaredFramework) !== normalize(actualFramework) };
}

function versionDto(v: any, declaredFramework: string) {
  const fidelity = architectureFidelity(declaredFramework, v.actual_framework);
  return {
    v: v.v, alias: v.alias, note: v.note,
    declaredFramework,
    actualFramework: v.actual_framework ?? undefined,
    actualArchitecture: v.actual_architecture ?? undefined,
    architectureFidelityChecked: fidelity.checked,
    architectureFidelityMismatch: fidelity.mismatch,
    // Real reproducibility lineage — genuinely captured and persisted at
    // registration time (modelRegistration.ts's captureReproducibility())
    // but, until now, never actually returned by either model_versions
    // route: the list query used SELECT * (so the data WAS fetched, just
    // dropped here in the DTO) but the detail query's explicit column
    // list never selected these columns at all — the write side worked,
    // the read side didn't exist. Found while live-testing v2.37.0's own
    // reproducibility claim.
    datasetSnapshotVersion: v.dataset_snapshot_version ?? undefined,
    datasetChecksum: v.dataset_checksum ?? undefined,
    reproducibility: v.reproducibility ?? undefined,
  };
}

// GET /api/models — catalog entries (deployed instances + adapters), each with version history
//
// Also carries the two things the Model catalog's lifecycle tabs need and the
// `catalog_models` table alone cannot answer:
//
//  * `training` / `trainingRunId` — whether a run is in flight for this model
//    right now. That lives in `training_runs`, not in the catalog, so without
//    the join the "In-training" tab could never have a member.
//  * entries for models that are being trained for the FIRST time. Those have
//    no `catalog_models` row at all until registration succeeds, so a model
//    would be invisible for the whole duration of its first run and then
//    appear, already finished. They're emitted here with `versions: []` and
//    `pending: true` — a real queued/running row, not a placeholder.
//
// `latestRunId` is the click-through target: the in-flight run when there is
// one, otherwise the most recent run that produced this model. Null for models
// with no run at all (seeded/imported entries), and the UI falls back to the
// version drawer for those rather than routing to a page that can't load.
modelsRouter.get("/models", requireAnyPermission(PERM.modelCatalog, PERM.modelOverview, PERM.dashboard, PERM.endpoints, PERM.train), async (_req: Request, res: Response) => {
  const models = await pool.query("SELECT * FROM catalog_models ORDER BY id");
  const versions = await pool.query("SELECT * FROM model_versions ORDER BY model_id, v DESC");
  // One row per model_name: the newest run, plus the newest run that is still
  // in flight. DISTINCT ON over an ordered scan rather than a correlated
  // subquery per model — this is on the catalog's landing path.
  const runs = await pool.query(
    `SELECT DISTINCT ON (model_name)
            model_name, id AS latest_run_id, status AS latest_status, base_model_id,
            compute_profile, dataset_name, created_at
       FROM training_runs
      ORDER BY model_name, created_at DESC`
  );
  const deployed = await pool.query("SELECT model_id, current_version FROM deployments");
  const inflight = await pool.query(
    `SELECT DISTINCT ON (model_name)
            model_name, id AS run_id, status, base_model_id, compute_profile, dataset_name, created_at
       FROM training_runs
      WHERE status IN ('queued', 'running')
      ORDER BY model_name, created_at DESC`
  );

  const latestByModel = new Map(runs.rows.map((r) => [r.model_name, r]));
  const inflightByModel = new Map(inflight.rows.map((r) => [r.model_name, r]));
  const deployedByModel = new Map(deployed.rows.map((r) => [r.model_id, r.current_version]));

  const withVersions = models.rows.map((m) => {
    const live = inflightByModel.get(m.id);
    const latest = latestByModel.get(m.id);
    return {
      ...m,
      versions: versions.rows
        .filter((v) => v.model_id === m.id)
        .map((v) => versionDto(v, m.framework)),
      training: Boolean(live),
      trainingRunId: live?.run_id ?? null,
      trainingStatus: live?.status ?? null,
      latestRunId: live?.run_id ?? latest?.latest_run_id ?? null,
      pending: false,
      // "Deployed" is a fact about serving, not about an alias. A deployments
      // row means something is genuinely behind an endpoint; a champion alias
      // means a version was promoted. Either is enough, and neither is implied
      // by registration — in this platform a newly-trained version is
      // registered as `challenger`, which is a promotion CANDIDATE that has
      // never served a request. Treating that as deployed (an earlier version
      // of this page did) put every model ever trained under "Deployed" and
      // left "Trained" permanently empty.
      deployed:
        deployedByModel.has(m.id) ||
        versions.rows.some((v) => v.model_id === m.id && v.alias === "champion"),
      deployedVersion: deployedByModel.get(m.id) ?? null,
    };
  });

  // First-time training runs: nothing in catalog_models yet. Everything shown
  // comes off the run row itself or the base model it was launched from — no
  // invented task/framework for a model that hasn't registered one.
  const known = new Set(models.rows.map((m) => m.id));
  const orphans = inflight.rows.filter((r) => !known.has(r.model_name));
  if (orphans.length) {
    const bases = await pool.query("SELECT id, domain, task, framework FROM base_models WHERE id = ANY($1::text[])", [
      [...new Set(orphans.map((o) => o.base_model_id))],
    ]);
    const baseById = new Map(bases.rows.map((b) => [b.id, b]));
    for (const o of orphans) {
      const b = baseById.get(o.base_model_id);
      withVersions.push({
        id: o.model_name,
        task: b?.task ?? "—",
        domain: b?.domain ?? "ml",
        kind: "trained",
        framework: b?.framework ?? "—",
        base_ref: o.base_model_id,
        updated: o.created_at,
        versions: [],
        training: true,
        trainingRunId: o.run_id,
        trainingStatus: o.status,
        latestRunId: o.run_id,
        deployed: false,
        deployedVersion: null,
        // Distinguishes "training its first version" from "registered, and
        // also training a new one" — the drawer says so rather than showing
        // an empty version history with no explanation.
        pending: true,
      });
    }
    withVersions.sort((a, b2) => String(a.id).localeCompare(String(b2.id)));
  }

  res.json(withVersions);
});

/**
 * GET /api/models/overview — one card per model for the Model Overview grid.
 *
 * Exists because the grid previously had no endpoint at all: the page could
 * only fetch /models/:id/monitoring, so rendering N cards meant N round trips
 * and the landing view was skipped entirely in favour of a dropdown.
 *
 * Everything here is read from real rows. `healthScore` and `status` come from
 * the monitoring record the serving path maintains; a model that has never
 * served a request genuinely has no score, and is reported as
 * `status: "insufficient-data"` with a null score rather than a zero — zero is
 * a health verdict, "not measured yet" is not.
 */
modelsRouter.get("/models/overview", requirePermission(PERM.modelOverview), async (_req: Request, res: Response) => {
  const result = await pool.query(
    `SELECT c.id, c.task, c.domain, c.kind, c.framework, c.updated,
            d.current_version, d.runtime, d.health_status AS deployment_health,
            mm.model_type, mm.health_score,
            mm.overview->'health'->>'status'        AS health_status,
            mm.overview->'health'->>'confidencePct' AS coverage_pct,
            (SELECT alias FROM model_versions v
              WHERE v.model_id = c.id AND v.v = COALESCE(d.current_version, (SELECT MAX(v2.v) FROM model_versions v2 WHERE v2.model_id = c.id))
              LIMIT 1) AS alias,
            COALESCE(d.current_version, (SELECT MAX(v3.v) FROM model_versions v3 WHERE v3.model_id = c.id)) AS shown_version
       FROM catalog_models c
       LEFT JOIN deployments d       ON d.model_id = c.id
       LEFT JOIN model_monitoring mm ON mm.model_id = c.id
      ORDER BY c.id`
  );

  res.json(
    result.rows.map((r) => {
      const scored = r.health_status && r.health_status !== "insufficient-data";
      return {
        modelId: r.id,
        task: r.task,
        domain: r.domain,
        kind: r.kind,
        framework: r.framework,
        updated: r.updated,
        // The badge the mock shows: the monitoring record's own model_type
        // where one exists, else derived from the catalog task.
        category: r.model_type
          ? r.model_type.replace(/_/g, " ")
          : /regress|forecast/i.test(r.task) ? "regression"
          : r.domain === "llm" ? "generation"
          : "classification",
        version: r.shown_version,
        alias: r.alias,
        deployed: r.current_version != null,
        runtime: r.runtime,
        deploymentHealth: r.deployment_health,
        // Null, not 0, when nothing has been measured — see above.
        healthScore: scored ? r.health_score : null,
        status: r.health_status || "insufficient-data",
        coveragePct: r.coverage_pct != null ? Number(r.coverage_pct) : null,
      };
    })
  );
});

// PATCH /api/models/:id — edit a registered model's description.
modelsRouter.patch("/models/:id", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const description = req.body?.description;
  if (typeof description !== "string" || description.length > 4000) {
    return res.status(400).json({ error: "description must be text of at most 4,000 characters" });
  }
  const r = await pool.query("UPDATE catalog_models SET description = $1 WHERE id = $2 RETURNING id, description", [description.trim() || null, req.params.id]);
  if (!r.rows.length) return res.status(404).json({ error: "model not found" });
  res.json(r.rows[0]);
});

modelsRouter.get("/models/:id", requireAnyPermission(PERM.modelCatalog, PERM.modelOverview), async (req: Request, res: Response) => {
  const modelResult = await pool.query("SELECT * FROM catalog_models WHERE id = $1", [req.params.id]);
  if (modelResult.rows.length === 0) return res.status(404).json({ error: "model not found" });
  const versionsResult = await pool.query(
    "SELECT v, alias, note, actual_framework, actual_architecture, dataset_snapshot_version, dataset_checksum, reproducibility FROM model_versions WHERE model_id = $1 ORDER BY v DESC",
    [req.params.id]
  );
  res.json({ ...modelResult.rows[0], versions: versionsResult.rows.map((v) => versionDto(v, modelResult.rows[0].framework)) });
});

// GET /api/models/:id/governance — real data from the Unified Catalog
// Service: lineage graph/runs/impact, plus the object profile (tags,
// classifications, entitlements). Each section degrades independently —
// UCS being unreachable, or this specific model never having been
// registered there yet, shows an honest "unavailable" for that section
// rather than 500ing the whole tab. Mirrors asset_profile_service.build's
// own per-section fail-soft pattern in the real UCS codebase.
modelsRouter.get("/models/:id/governance", requireAnyPermission(PERM.modelOverview, PERM.modelCatalog), async (req: Request, res: Response) => {
  const modelResult = await pool.query("SELECT id FROM catalog_models WHERE id = $1", [req.params.id]);
  if (modelResult.rows.length === 0) return res.status(404).json({ error: "model not found" });

  const fqn = modelFqn(req.params.id);
  const identity = ucsIdentityFromPrincipal(req.principal);

  async function safe<T>(label: string, fn: () => Promise<T>): Promise<{ status: "available"; data: T } | { status: "unavailable"; reason: string }> {
    try {
      return { status: "available", data: await fn() };
    } catch (err) {
      return { status: "unavailable", reason: err instanceof UcsError ? err.message : `${label} failed: ${String(err)}` };
    }
  }

  const [lineage, runs, impact, profile] = await Promise.all([
    safe("lineage graph", () => getLineageGraph(identity, fqn)),
    safe("lineage runs", () => getLineageRuns(identity, fqn)),
    safe("lineage impact", () => getLineageImpact(identity, fqn)),
    safe("object profile", () => getObjectProfile(identity, fqn)),
  ]);

  res.json({ fqn, lineage, runs, impact, profile });
});
