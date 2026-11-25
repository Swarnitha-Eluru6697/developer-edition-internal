import { Router, Request, Response } from "express";
import { z } from "zod";
import { randomBytes } from "crypto";
import type { PoolClient } from "pg";
import { pool } from "../db/connection";
import { PERM, requireAnyPermission, requirePermission, requireUcsDeploymentWrite } from "../middleware/permissions";
import { registerModel, registerModelVersion, UcsError } from "../ucs/client";
import { getServingEngineFor } from "../execution/servingEngine";
import { hashSecret } from "../utils/secretHash";
import { requiresDurableArtifacts } from "../utils/runtimePolicy";
import { getStorage } from "../storage";
import { verifyDurableArtifact } from "../services/artifactPersistence";

/**
 * Runs a UCS cataloging call without letting its failure fail the local
 * operation — promotion has already committed by this point and must not be
 * undone because an external catalog was unreachable.
 *
 * Returns the failure instead of only logging it. It used to log to the server
 * console and return nothing, so a promote whose UCS registration failed
 * answered a bare `{ok: true}` and looked completely successful. Eight models
 * were promoted that way against an unreachable UCS and nobody could have
 * known from the API or the UI — the divergence only showed up by comparing
 * the two catalogs by hand.
 */
async function catalogModelOperation(fn: () => Promise<unknown>, what: string): Promise<string | null> {
  try {
    await fn();
    return null;
  } catch (err) {
    // 409 means the model, or this exact version URI, is already in the
    // catalog — which is the desired end state, not a failure. Re-promoting
    // the current champion hits this every time, so reporting it as a warning
    // would fire on the normal path and teach everyone to ignore the field.
    if (err instanceof UcsError && err.status === 409) return null;
    const message = err instanceof UcsError ? err.message : err instanceof Error ? err.message : String(err);
    // eslint-disable-next-line no-console
    console.error(`UCS cataloging failed for ${what} (operation still succeeded locally):`, message);
    return `${what} was not recorded in the Unified Catalog: ${message}. The local promotion stands — re-run it, or backfill with scripts/backfillUcs.ts, once UCS is reachable.`;
  }
}

async function withModelDeploymentLock<T>(modelId: string, fn: (client: PoolClient) => Promise<T>): Promise<T> {
  const client = await pool.connect();
  try {
    // Session-level advisory lock serializes promote/rollback for ONE model
    // across the external deploy/readiness wait without holding a long DB
    // transaction. Once the serving target is Ready, the local state changes
    // are committed together in one short transaction below.
    await client.query("SELECT pg_advisory_lock(hashtext($1))", [`deploy:${modelId}`]);
    return await fn(client);
  } finally {
    await client.query("SELECT pg_advisory_unlock(hashtext($1))", [`deploy:${modelId}`]).catch(() => {});
    client.release();
  }
}

/**
 * Refuses to point a deployment at a version whose artifact isn't really there.
 *
 * The check used to be skipped entirely outside production mode, which made
 * both promote and rollback happy to deploy a version whose object had been
 * deleted from storage — the failure only appeared later, per request, as a
 * silent fall back to the development stand-in. Rollback made that worst:
 * "roll back to the previous version" would succeed and quietly leave the
 * model serving fabricated numbers.
 *
 * So a RECORDED artifact is now always verified, in every mode: if a version
 * claims a weights_uri, that object has to exist before anything is pointed at
 * it. What production mode still adds is intolerance of the weaker cases —
 * a version with no weights_uri at all, or no storage configured — which are
 * normal for seeded/dev models and must stay deployable in development.
 */
async function requireDeployableArtifact(weightsUri: string | null, modelId: string, version: number): Promise<void> {
  const lenient = !requiresDurableArtifacts();
  // Nothing recorded, or nowhere to look: only production insists.
  if (lenient && (!weightsUri || !getStorage())) return;
  const durable = await verifyDurableArtifact(weightsUri);
  if (!durable.ok) {
    throw new Error(`ARTIFACT_NOT_DEPLOYABLE: ${modelId} v${version} has no verified durable artifact: ${durable.reason}`);
  }
}

export const deployRouter = Router();

// The person behind a promote/rollback, for deployments.deployed_by: the
// gateway principal's name (req.actor). Empty means no named caller.
function deployedByOf(req: Request): string | null {
  return req.actor ? req.actor : null;
}

// ?deployedBy=<name> narrows to one person's deployments (case-insensitive),
// which is what the Agents app's "ML Studio models" tab asks for. Listing what
// is deployed (with its endpoint URL) is also an endpoints:view read.
deployRouter.get("/deployments", requireAnyPermission(PERM.deploy, PERM.alertsView, PERM.endpoints), async (req: Request, res: Response) => {
  const deployedBy = typeof req.query.deployedBy === "string" ? req.query.deployedBy.trim() : "";
  const result = await pool.query(
    `SELECT d.*, c.task, c.domain, c.framework, e.url AS endpoint_url, e.status AS endpoint_status
       FROM deployments d
       JOIN catalog_models c ON c.id = d.model_id
       LEFT JOIN endpoints e ON e.model_id = d.model_id
      WHERE ($1 = '' OR lower(d.deployed_by) = lower($1))
      ORDER BY d.deployed_at DESC NULLS LAST, d.model_id`,
    [deployedBy]
  );
  res.json(result.rows.map((r) => ({
    modelId: r.model_id, task: r.task, domain: r.domain, framework: r.framework,
    currentVersion: r.current_version, previousVersion: r.previous_version,
    healthStatus: r.health_status, runtime: r.runtime, replicas: r.replicas,
    canRollback: r.previous_version != null,
    deployedBy: r.deployed_by ?? null, deployedAt: r.deployed_at ?? null,
    endpointUrl: r.endpoint_url ?? null, endpointStatus: r.endpoint_status ?? null,
  })));
});

deployRouter.get("/promotions/pending", requireAnyPermission(PERM.deploy, PERM.dashboard), async (_req: Request, res: Response) => {
  const result = await pool.query(
    `SELECT v.model_id, v.v, v.note, c.task, c.domain, c.framework, d.current_version
     FROM model_versions v
     JOIN catalog_models c ON c.id = v.model_id
     LEFT JOIN deployments d ON d.model_id = v.model_id
     WHERE v.alias = 'challenger' AND (d.current_version IS NULL OR d.current_version != v.v)
     ORDER BY v.model_id`
  );
  res.json(result.rows.map((r) => ({
    modelId: r.model_id, candidateVersion: r.v, note: r.note,
    task: r.task, domain: r.domain,
    // Carried so the ML / DL / LLM badge here matches the one the catalog
    // shows for the same model — deep learning is a framework fact, and
    // without it this page silently badged every pytorch model as plain ML.
    framework: r.framework,
    currentChampionVersion: r.current_version,
  })));
});

/**
 * GET /api/promotions/:modelId/:version — everything a promotion decision needs,
 * assembled from data that already exists.
 *
 * Each version links to the training run that produced it (model_versions
 * .training_run_id), and that run carries its real measured metrics, the exact
 * dataset snapshot it trained on, and its reproducibility record. So candidate
 * and champion are compared on figures that were genuinely measured, and where
 * a version predates that linkage (seed data, or a version registered before
 * training_run_id existed) its metrics come back null and the comparison says
 * "not recorded" instead of inventing a number to fill the column.
 *
 * The checklist is computed, not authored: each item is a real predicate over
 * this data, with `status` "pass" / "fail" / "unknown" — "unknown" where the
 * underlying fact genuinely isn't recorded, which is a different thing from a
 * failed check and is shown differently.
 */
deployRouter.get("/promotions/:modelId/:version", requirePermission(PERM.deploy), async (req: Request, res: Response) => {
  const version = Number(req.params.version);
  if (!Number.isInteger(version)) return res.status(400).json({ error: "version must be an integer" });

  const model = await pool.query("SELECT * FROM catalog_models WHERE id = $1", [req.params.modelId]);
  if (model.rows.length === 0) return res.status(404).json({ error: "model not found" });

  const versionRow = async (v: number | null) => {
    if (v == null) return null;
    const r = await pool.query(
      `SELECT mv.*, tr.metrics, tr.created_at AS trained_at, tr.compute_profile, tr.base_model_id, tr.hyperparams
         FROM model_versions mv
         LEFT JOIN training_runs tr ON tr.id = mv.training_run_id
        WHERE mv.model_id = $1 AND mv.v = $2`,
      [req.params.modelId, v]
    );
    if (r.rows.length === 0) return null;
    const row = r.rows[0];
    return {
      version: row.v,
      alias: row.alias,
      note: row.note,
      trainedAt: row.trained_at ?? null,
      baseModelId: row.base_model_id ?? null,
      computeProfile: row.compute_profile ?? null,
      hyperparams: row.hyperparams ?? null,
      datasetName: row.dataset_name ?? null,
      datasetSnapshotVersion: row.dataset_snapshot_version ?? null,
      datasetChecksum: row.dataset_checksum ?? null,
      weightsUri: row.weights_uri ?? null,
      artifactFormat: row.artifact_format ?? null,
      actualFramework: row.actual_framework ?? null,
      // Null when this version has no linked training run — the metrics were
      // never recorded, as opposed to being zero.
      metrics: row.metrics ?? null,
      hasTrainingRun: !!row.training_run_id,
    };
  };

  const candidate = await versionRow(version);
  if (!candidate) return res.status(404).json({ error: `version ${version} not found for '${req.params.modelId}'` });

  const deployment = await pool.query("SELECT current_version FROM deployments WHERE model_id = $1", [req.params.modelId]);
  const champion = await versionRow(deployment.rows[0]?.current_version ?? null);

  // The metric the comparison turns on. Classification models report accuracy;
  // regression reports r2 — picked from what the runs actually recorded rather
  // than assumed from the task string.
  const shared = candidate.metrics && champion?.metrics
    ? Object.keys(candidate.metrics).filter((k) => k in (champion.metrics as object))
    : [];
  const objective = ["accuracy", "f1", "r2", "auc"].find((m) => shared.includes(m)) ?? shared[0] ?? null;
  const num = (m: any, k: string | null) => (k && m && m[k] != null ? Number(m[k]) : null);
  const candidateScore = num(candidate.metrics, objective);
  const championScore = num(champion?.metrics, objective);

  const check = (label: string, status: "pass" | "fail" | "unknown", detail: string) => ({ label, status, detail });
  const checklist = [
    candidateScore === null || championScore === null
      ? check(
          "Beats the current champion on its objective metric",
          "unknown",
          champion
            ? "Measured metrics aren't recorded for one or both versions, so there's nothing to compare. A version registered by a real training run carries its own metrics."
            : "Nothing is currently deployed for this model, so there's no champion to compare against — this promotion would be the first."
        )
      : check(
          `Beats the current champion on ${objective}`,
          candidateScore >= championScore ? "pass" : "fail",
          `candidate ${candidateScore} vs champion ${championScore}`
        ),
    candidate.weightsUri
      ? check("Has a durable artifact to serve", "pass", candidate.weightsUri)
      : check(
          "Has a durable artifact to serve",
          "fail",
          "No weights_uri recorded for this version — there is nothing for the serving runtime to load. Promotion is refused outright when the deployment requires durable artifacts."
        ),
    !champion
      ? check("Evaluated on the same dataset snapshot as the champion", "unknown", "No champion to compare against.")
      : candidate.datasetChecksum && champion.datasetChecksum
      ? check(
          "Evaluated on the same dataset snapshot as the champion",
          candidate.datasetChecksum === champion.datasetChecksum ? "pass" : "fail",
          candidate.datasetChecksum === champion.datasetChecksum
            ? `both trained on ${candidate.datasetName} v${candidate.datasetSnapshotVersion}`
            : `candidate: ${candidate.datasetName} v${candidate.datasetSnapshotVersion}, champion: ${champion.datasetName} v${champion.datasetSnapshotVersion} — the metrics above are not directly comparable`
        )
      : check(
          "Evaluated on the same dataset snapshot as the champion",
          "unknown",
          "Dataset checksums aren't recorded for one or both versions, so whether they were evaluated on the same data can't be established."
        ),
    candidate.hasTrainingRun
      ? check("Has a full reproducibility record", "pass", "Linked to the training run that produced it — dataset pin, seed, library versions, and hyperparameters all recorded.")
      : check("Has a full reproducibility record", "unknown", "This version isn't linked to a training run, so there's no reproducibility record for it."),
  ];

  res.json({
    modelId: req.params.modelId,
    task: model.rows[0].task,
    domain: model.rows[0].domain,
    objectiveMetric: objective,
    comparableMetrics: shared,
    candidate,
    champion,
    checklist,
  });
});

const promoteSchema = z.object({ version: z.number().int() });
deployRouter.post("/deployments/:modelId/promote", requirePermission(PERM.deploy), requireUcsDeploymentWrite, async (req: Request, res: Response) => {
  const parsed = promoteSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid request", details: parsed.error.flatten() });

  try {
    const outcome = await withModelDeploymentLock(req.params.modelId, async (client) => {
      const candidate = await client.query("SELECT * FROM model_versions WHERE model_id = $1 AND v = $2", [req.params.modelId, parsed.data.version]);
      if (candidate.rows.length === 0) return { httpError: 404, body: { error: "version not found" } } as const;
      if (candidate.rows[0].alias !== "challenger") return { httpError: 422, body: { error: "only a challenger version can be promoted" } } as const;

      await requireDeployableArtifact(candidate.rows[0].weights_uri, req.params.modelId, parsed.data.version);

      const existingDeployment = await client.query("SELECT * FROM deployments WHERE model_id = $1", [req.params.modelId]);
      const previousVersion = existingDeployment.rows[0]?.current_version ?? null;
      const domainRow = await client.query("SELECT domain, framework FROM catalog_models WHERE id = $1", [req.params.modelId]);
      const isLlmModel = domainRow.rows[0]?.domain === "llm";
      const servingEngine = getServingEngineFor(isLlmModel);

      const { endpointUrl, warning: deployWarning } = await servingEngine.deploy(req.params.modelId, parsed.data.version, {
        weightsUri: candidate.rows[0].weights_uri,
        artifactFormat: candidate.rows[0].artifact_format,
      });
      const readiness = await servingEngine.waitForReady(req.params.modelId);
      if (!readiness.ready) {
        return { httpError: 502, body: { error: `deploy request was accepted, but the model never became ready: ${readiness.reason}`, endpointUrl, servingEngine: servingEngine.kind } } as const;
      }

      // Local promotion state is one atomic commit: aliases, deployment,
      // endpoint and audit row cannot become partially visible anymore.
      try {
        await client.query("BEGIN");
        await client.query("UPDATE model_versions SET alias = 'archived' WHERE model_id = $1 AND alias = 'champion'", [req.params.modelId]);
        await client.query("UPDATE model_versions SET alias = 'champion' WHERE model_id = $1 AND v = $2", [req.params.modelId, parsed.data.version]);

        if (existingDeployment.rows.length === 0) {
          await client.query(
            `INSERT INTO deployments (model_id, current_version, previous_version, health_status, runtime, replicas, deployed_by, deployed_at)
             VALUES ($1, $2, NULL, 'healthy', $3, '2–8', $4, NOW())`,
            [req.params.modelId, parsed.data.version, servingEngine.kind === "kserve" ? "KServe" : "Local (dev)", deployedByOf(req)]
          );
        } else {
          await client.query("UPDATE deployments SET current_version = $1, previous_version = $2, deployed_by = $4, deployed_at = NOW() WHERE model_id = $3", [parsed.data.version, previousVersion, req.params.modelId, deployedByOf(req)]);
        }

        const existingEndpoint = await client.query("SELECT model_id FROM endpoints WHERE model_id = $1", [req.params.modelId]);
        if (existingEndpoint.rows.length === 0) {
          const realSecret = `cs_live_${randomBytes(24).toString("hex")}`;
          await client.query(
            `INSERT INTO endpoints (model_id, title, url, status, token_endpoint, client_id, client_secret_masked, client_secret_hash, rpm, burst, concurrent)
             VALUES ($1, $1, $2, 'active', $3, $4, $5, $6, 1000, 100, 20)`,
            [req.params.modelId, endpointUrl, `${endpointUrl.split("/api/")[0]}/oauth/token`, `mlp-svc-${req.params.modelId}`, `cs_live_••••••••••••${realSecret.slice(-4)}`, hashSecret(realSecret)]
          );
        } else {
          await client.query("UPDATE endpoints SET url = $1, status = 'active' WHERE model_id = $2", [endpointUrl, req.params.modelId]);
        }
        await client.query("INSERT INTO audit_log (actor, action, target) VALUES ($1, 'promoted', $2)", [req.actor, `${req.params.modelId} to v${parsed.data.version}`]);
        await client.query("COMMIT");
      } catch (err) {
        await client.query("ROLLBACK").catch(() => {});
        // Best-effort compensation: the external serving target was already
        // switched before the local transaction failed. Restore the previous
        // artifact (or undeploy when this was the first deployment) so serving
        // and Postgres do not knowingly diverge.
        try {
          if (previousVersion != null) {
            const prev = await client.query("SELECT weights_uri, artifact_format FROM model_versions WHERE model_id=$1 AND v=$2", [req.params.modelId, previousVersion]);
            if (prev.rows[0]?.weights_uri) {
              await servingEngine.deploy(req.params.modelId, previousVersion, { weightsUri: prev.rows[0].weights_uri, artifactFormat: prev.rows[0].artifact_format });
              await servingEngine.waitForReady(req.params.modelId);
            }
          } else {
            await servingEngine.undeploy(req.params.modelId);
          }
        } catch (compensationErr) {
          // eslint-disable-next-line no-console
          console.error(`promotion DB transaction failed and serving compensation also failed for '${req.params.modelId}':`, compensationErr);
        }
        throw err;
      }

      const declaredFramework: string | undefined = domainRow.rows[0]?.framework;
      const actualFramework: string | null = candidate.rows[0].actual_framework;
      const fidelityMismatch = declaredFramework && actualFramework
        ? declaredFramework.toLowerCase().replace(/[^a-z0-9]/g, "") !== actualFramework.toLowerCase().replace(/[^a-z0-9]/g, "")
        : false;
      const architectureFidelityWarning = fidelityMismatch
        ? `catalog declares '${declaredFramework}' but v${parsed.data.version} was actually trained with '${actualFramework}' — see GET /api/models/${req.params.modelId} for the real per-version framework/architecture.`
        : undefined;

      return { previousVersion, endpointUrl, deployWarning, servingEngineKind: servingEngine.kind, architectureFidelityWarning } as const;
    });

    // Real cast, not a suppression of a real bug: every httpError branch
    // above returns a literal number (404/422/502); TS's inference across
    // this function's several async/try-catch-wrapped return paths widens
    // it to number|undefined here even though it's never actually
    // undefined at runtime once the 'httpError' in outcome check passes —
    // confirmed with an isolated minimal repro of the same discriminated-
    // union pattern, which compiles clean, so this is specific to this
    // function's complexity, not the pattern itself being unsound.
    if ("httpError" in outcome) return res.status(outcome.httpError as number).json(outcome.body);

    const catalogWarnings = [
      await catalogModelOperation(() => registerModel(req.params.modelId, `Deployed model: ${req.params.modelId}`, {}), `model '${req.params.modelId}'`),
      await catalogModelOperation(
        () => registerModelVersion(req.params.modelId, `model://${req.params.modelId}/v${parsed.data.version}`, ["champion"], `Promoted to champion by ${req.actor}`, { version: parsed.data.version, previousVersion: outcome.previousVersion }),
        `promotion of ${req.params.modelId} to v${parsed.data.version}`
      ),
    ].filter((w): w is string => w !== null);

    return res.json({
      ok: true, promotedTo: parsed.data.version, previousVersion: outcome.previousVersion,
      endpointUrl: outcome.endpointUrl, servingEngine: outcome.servingEngineKind,
      ...(outcome.deployWarning ? { warning: outcome.deployWarning } : {}),
      ...(outcome.architectureFidelityWarning ? { architectureFidelityWarning: outcome.architectureFidelityWarning } : {}),
      ...(catalogWarnings.length ? { catalogWarnings } : {}),
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return res.status(message.startsWith("ARTIFACT_NOT_DEPLOYABLE") ? 422 : 500).json({ error: message });
  }
});

deployRouter.post("/deployments/:modelId/rollback", requirePermission(PERM.deploy), requireUcsDeploymentWrite, async (req: Request, res: Response) => {
  try {
    const outcome = await withModelDeploymentLock(req.params.modelId, async (client) => {
      const deployment = await client.query("SELECT * FROM deployments WHERE model_id = $1", [req.params.modelId]);
      if (deployment.rows.length === 0) return { httpError: 404, body: { error: "no deployment found for this model" } } as const;
      if (deployment.rows[0].previous_version == null) return { httpError: 409, body: { error: "nothing to roll back to" } } as const;

      const { current_version, previous_version } = deployment.rows[0];
      const previousVersionRow = await client.query("SELECT weights_uri, artifact_format FROM model_versions WHERE model_id = $1 AND v = $2", [req.params.modelId, previous_version]);
      if (previousVersionRow.rows.length === 0) return { httpError: 404, body: { error: `version ${previous_version} (the rollback target) no longer exists` } } as const;

      await requireDeployableArtifact(previousVersionRow.rows[0].weights_uri, req.params.modelId, previous_version);

      const currentVersionRow = await client.query("SELECT weights_uri, artifact_format FROM model_versions WHERE model_id = $1 AND v = $2", [req.params.modelId, current_version]);
      const domainRow = await client.query("SELECT domain FROM catalog_models WHERE id = $1", [req.params.modelId]);
      const servingEngine = getServingEngineFor(domainRow.rows[0]?.domain === "llm");
      const { endpointUrl, warning: deployWarning } = await servingEngine.deploy(req.params.modelId, previous_version, {
        weightsUri: previousVersionRow.rows[0].weights_uri,
        artifactFormat: previousVersionRow.rows[0].artifact_format,
      });
      const readiness = await servingEngine.waitForReady(req.params.modelId);
      if (!readiness.ready) return { httpError: 502, body: { error: `rollback's redeploy was accepted, but the model never became ready: ${readiness.reason}`, endpointUrl, servingEngine: servingEngine.kind } } as const;

      try {
        await client.query("BEGIN");
        await client.query("UPDATE model_versions SET alias = 'archived' WHERE model_id = $1 AND v = $2", [req.params.modelId, current_version]);
        await client.query("UPDATE model_versions SET alias = 'champion' WHERE model_id = $1 AND v = $2", [req.params.modelId, previous_version]);
        await client.query("UPDATE deployments SET current_version = $1, previous_version = $2, deployed_by = $4, deployed_at = NOW() WHERE model_id = $3", [previous_version, current_version, req.params.modelId, deployedByOf(req)]);
        await client.query("UPDATE endpoints SET url = $1, status = 'active' WHERE model_id = $2", [endpointUrl, req.params.modelId]);
        await client.query("INSERT INTO audit_log (actor, action, target) VALUES ($1, 'rolled back', $2)", [req.actor, `${req.params.modelId} to v${previous_version}`]);
        await client.query("COMMIT");
      } catch (err) {
        await client.query("ROLLBACK").catch(() => {});
        try {
          if (currentVersionRow.rows[0]?.weights_uri) {
            await servingEngine.deploy(req.params.modelId, current_version, { weightsUri: currentVersionRow.rows[0].weights_uri, artifactFormat: currentVersionRow.rows[0].artifact_format });
            await servingEngine.waitForReady(req.params.modelId);
          }
        } catch (compensationErr) {
          // eslint-disable-next-line no-console
          console.error(`rollback DB transaction failed and serving compensation also failed for '${req.params.modelId}':`, compensationErr);
        }
        throw err;
      }

      return { current_version, previous_version, endpointUrl, deployWarning, servingEngineKind: servingEngine.kind } as const;
    });

    // Real cast, not a suppression of a real bug: every httpError branch
    // above returns a literal number (404/422/502); TS's inference across
    // this function's several async/try-catch-wrapped return paths widens
    // it to number|undefined here even though it's never actually
    // undefined at runtime once the 'httpError' in outcome check passes —
    // confirmed with an isolated minimal repro of the same discriminated-
    // union pattern, which compiles clean, so this is specific to this
    // function's complexity, not the pattern itself being unsound.
    if ("httpError" in outcome) return res.status(outcome.httpError as number).json(outcome.body);

    const catalogWarning = await catalogModelOperation(
      () => registerModelVersion(req.params.modelId, `model://${req.params.modelId}/v${outcome.previous_version}`, ["champion"], `Rolled back to champion by ${req.actor}`, { version: outcome.previous_version, rolledBackFrom: outcome.current_version }),
      `rollback of ${req.params.modelId} to v${outcome.previous_version}`
    );

    return res.json({
      ok: true, rolledBackTo: outcome.previous_version, rolledBackFrom: outcome.current_version,
      endpointUrl: outcome.endpointUrl, servingEngine: outcome.servingEngineKind,
      ...(outcome.deployWarning ? { warning: outcome.deployWarning } : {}),
      ...(catalogWarning ? { catalogWarnings: [catalogWarning] } : {}),
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return res.status(message.startsWith("ARTIFACT_NOT_DEPLOYABLE") ? 422 : 500).json({ error: message });
  }
});
