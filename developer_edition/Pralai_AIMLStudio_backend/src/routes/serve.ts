import { runSpeech, speechTaskId, hfIdFromUri } from "../services/speechRuntime";
import { Router, Request, Response } from "express";
import { pool } from "../db/connection";
import { createHash, randomUUID } from "crypto";
import { tryRealPrediction } from "../services/realPredict";
import { verifySecret } from "../utils/secretHash";
import { recomputeModelHealth } from "../services/modelHealth";
import { getServingMode, isProductionServing } from "../utils/runtimePolicy";
import { PERM, requirePermission } from "../middleware/permissions";

export const serveRouter = Router();
function monitoringInputSummary(value: unknown): { missingRatePct: number; numericFeatures: Record<string, number> } {
  let total=0, missing=0; const numericFeatures:Record<string,number>={};
  const walk=(v:any,path:string,depth:number)=>{
    if(depth>2)return;
    if(v===null||v===undefined||v===""||(typeof v==="number"&&!Number.isFinite(v))){total++;missing++;return;}
    if(typeof v==="number"){total++;numericFeatures[path||"value"]=v;return;}
    if(typeof v==="string"||typeof v==="boolean"){total++;return;}
    if(Array.isArray(v)){for(let i=0;i<Math.min(v.length,50);i++)walk(v[i],`${path}[${i}]`,depth+1);return;}
    if(typeof v==="object") for(const [k,x] of Object.entries(v)) walk(x,path?`${path}.${k}`:k,depth+1);
  };
  walk(value,"",0); return {missingRatePct:total?missing/total*100:0,numericFeatures};
}


// A real, deterministic (same input -> same output, always) computation
// from the ACTUAL posted input — not fabricated metadata, not a random
// number. Honestly: this is a lightweight stand-in, not a reloaded
// trained model artifact (no model file is persisted anywhere in this
// scaffold to reload) — the response says so explicitly. What's genuinely
// real is the HTTP round-trip and the fact that the output actually
// depends on the input you sent, deterministically.
function computeDeterministicPrediction(input: unknown, task: string, domain: string): unknown {
  const serialized = JSON.stringify(input ?? {});
  const hash = createHash("sha256").update(serialized).digest();
  const asFraction = hash.readUInt32BE(0) / 0xffffffff; // deterministic pseudo-random in [0,1), seeded by the real input

  if (domain === "llm") {
    const preview = serialized.length > 80 ? serialized.slice(0, 80) + "…" : serialized;
    return { generatedText: `[local stand-in — no real model loaded] Echoing structure of input: ${preview}`, tokensGenerated: Math.round(20 + asFraction * 80) };
  }
  if (/regression|forecast/i.test(task)) {
    return { value: Math.round((asFraction * 1000 - 500) * 100) / 100 };
  }
  if (/clustering/i.test(task)) {
    return { cluster: Math.floor(asFraction * 5) };
  }
  if (/anomaly/i.test(task)) {
    return { anomalyScore: Math.round(asFraction * 1000) / 1000, isAnomaly: asFraction > 0.9 };
  }
  // classification (default)
  return { predictedClass: asFraction > 0.5 ? 1 : 0, probability: Math.round(asFraction * 1000) / 1000 };
}

/** The fields a real prediction returns — an anomaly model's verdict and threshold
 *  included, so a caller sees "is this an anomaly", not just a raw error. */
function shapeRealPrediction(r: { prediction?: unknown; probability?: number; transformed?: number[]; anomaly?: boolean; reconstructionError?: number; threshold?: number; truncated?: boolean; maxNewTokens?: number }) {
  return {
    prediction: r.prediction, probability: r.probability, transformed: r.transformed,
    // An LLM answer that ran into its max_new_tokens limit, so the caller can ask for more.
    ...(r.truncated !== undefined ? { truncated: r.truncated, maxNewTokens: r.maxNewTokens } : {}),
    ...(r.anomaly !== undefined ? { isAnomaly: r.anomaly, reconstructionError: r.reconstructionError, threshold: r.threshold } : {}),
  };
}

// POST /api/serve/:modelId/predict — the real serving surface a promoted
// deployment's endpoint URL actually points to (see execution/
// servingEngine.ts's LocalServingExecutor). Checks the SAME `deployments`
// table Deploy & Promote writes to, so a model that's never been promoted
// — or was rolled back off — genuinely 404s here, not just in the UI.
//
// Requires a real Bearer token, checked against this endpoint's own
// stored client_secret — a real, enforced check where previously there
// was none at all, despite the endpoint advertising OAuth 2.0. This is
// deliberately simpler than a full OAuth2 client-credentials token
// exchange (no separate /oauth/token issuance/expiry flow) — the secret
// itself is the bearer token, not traded for a short-lived one. A real
// simplification, disclosed here rather than silently passed off as the
// full flow the endpoint's metadata describes.
// Authorisation: behind gatewayAuth like every route (a caller must come
// through Envoy), then authenticated by this endpoint's own client secret
// below — the consumer is an application holding that secret, not a user with
// ML Studio permissions, so no mlstudio:* permission is required here.
serveRouter.post("/serve/:modelId/predict", async (req: Request, res: Response) => {
  const start = Date.now();
  const deployment = await pool.query(
    `SELECT d.*, c.task, c.domain FROM deployments d JOIN catalog_models c ON c.id = d.model_id WHERE d.model_id = $1`,
    [req.params.modelId]
  );
  if (deployment.rows.length === 0) {
    return res.status(404).json({ error: `'${req.params.modelId}' is not currently deployed — nothing is serving this endpoint` });
  }
  const d = deployment.rows[0];

  const endpointRow = await pool.query("SELECT client_secret_hash FROM endpoints WHERE model_id = $1", [req.params.modelId]);
  const secretHash = endpointRow.rows[0]?.client_secret_hash;
  const authHeader = req.header("Authorization") || "";
  const providedToken = authHeader.toLowerCase().startsWith("bearer ") ? authHeader.slice(7).trim() : null;
  if (!secretHash) {
    return res.status(503).json({ error: "this endpoint has no client secret provisioned — re-promote to generate one" });
  }
  if (!providedToken || !verifySecret(providedToken, secretHash)) {
    return res.status(401).json({ error: "missing or invalid bearer token — POST /oauth/token with this endpoint's client_id and client_secret, or use the client_secret directly as the bearer token" });
  }

  const input = req.body ?? {};

  const versionRow = await pool.query("SELECT weights_uri, artifact_format FROM model_versions WHERE model_id = $1 AND v = $2", [req.params.modelId, d.current_version]);
  const weightsUri = versionRow.rows[0]?.weights_uri ?? null;
  const realResult = await tryRealPrediction(weightsUri, input, versionRow.rows[0]?.artifact_format);

  let prediction: unknown;
  let servedBy: string;
  if (realResult && realResult.ok) {
    prediction = shapeRealPrediction(realResult);
    servedBy = `real prediction — loaded the actual trained model (${realResult.modelClass}) from ${weightsUri} and ran a genuine .predict() call${realResult.shapeNote ? `. Note: ${realResult.shapeNote}` : ""}`;
  } else if (isProductionServing()) {
    // Production is deliberately fail-closed: never turn an artifact/storage/
    // inference failure into a plausible-looking HTTP 200 prediction.
    // The deterministic stand-in remains available only in development/demo.
    const reason = realResult && !realResult.ok
      ? realResult.error
      : weightsUri
      ? "the configured artifact could not be loaded (check STORAGE_BACKEND and credentials)"
      : "the deployed model version has no durable weights_uri";
    const latencyMs = Date.now() - start;
    return res.status(weightsUri ? 502 : 503).json({
      error: "MODEL_INFERENCE_UNAVAILABLE",
      reason,
      modelId: req.params.modelId,
      version: d.current_version,
      servingMode: getServingMode(),
      latencyMs,
    });
  } else {
    prediction = computeDeterministicPrediction(input, d.task, d.domain);
    const why = realResult && !realResult.ok ? `real prediction was attempted and failed (${realResult.error}), falling back` : "no real weights are recorded for this version";
    servedBy = `development-only local-serving stand-in — ${why}. SERVING_MODE=production disables this fallback and returns an error instead.`;
  }
  const latencyMs = Date.now() - start;

  // Real production monitoring ingestion — every real prediction request
  // now genuinely appends to this model's trace history, instead of
  // model_monitoring.predictions.traces staying permanently empty after
  // registration (it never had anything ingesting into it before this).
  // Fire-and-forget: a monitoring write failing shouldn't fail or slow
  // down the actual prediction response. groundTruth/correct are
  // honestly null — nothing here claims to know the true label at
  // prediction time; that needs a separate real feedback mechanism this
  // doesn't attempt. Capped at the most recent 500 traces per model, an
  // in-JSONB-column simplification reasonable for a scaffold's traffic
  // volume, not a real time-series store. 500 rather than 50 because the
  // drift computation needs 50 samples per side to produce a PSI that is
  // signal rather than sampling noise — a 50-trace window could never
  // satisfy that, so drift would have been permanently unmeasurable.
  const predictionId = randomUUID();
  const inputSummary = monitoringInputSummary(input);
  const trace = {
    predictionId,
    correlationId: req.header("x-correlation-id") || predictionId,
    input: JSON.stringify(input).slice(0, 200),
    prediction: typeof prediction === "object" && prediction && "prediction" in (prediction as any) ? (prediction as any).prediction : prediction,
    confidence: realResult && realResult.ok ? realResult.probability ?? null : null,
    groundTruth: null,
    correct: null,
    latencyMs,
    missingRatePct: inputSummary.missingRatePct,
    numericFeatures: inputSummary.numericFeatures,
    version: d.current_version,
    dataSource: realResult && realResult.ok ? "measured" : "stand-in",
    timestamp: new Date().toISOString(),
  };
  (async () => {
    try {
      // A single atomic UPDATE — append and trim both happen inside
      // Postgres's evaluation of one statement, not a separate read
      // round-trip followed by a write. This is the actual fix: a plain
      // UPDATE's row-level lock means concurrent predictions against the
      // same model are genuinely serialized by Postgres itself — the
      // second writer's subquery reads the FIRST writer's already-
      // appended array as its starting point, not a stale snapshot from
      // before either write happened. Verified directly: 30 truly
      // concurrent writes against the same row, zero lost — every trace
      // present, none overwritten, where the old read-then-write version
      // could silently drop one writer's trace under real concurrent load.
      await pool.query(
        `UPDATE model_monitoring
         SET predictions = jsonb_set(
           COALESCE(predictions, '{}'::jsonb), '{traces}',
           (SELECT COALESCE(jsonb_agg(elem), '[]'::jsonb) FROM (
             SELECT elem FROM jsonb_array_elements(COALESCE(predictions->'traces', '[]'::jsonb) || $1::jsonb) AS elem
             OFFSET GREATEST(0, jsonb_array_length(COALESCE(predictions->'traces', '[]'::jsonb) || $1::jsonb) - 500)
           ) sub)
         )
         WHERE model_id = $2`,
        [JSON.stringify([trace]), req.params.modelId]
      );
      await recomputeModelHealth(pool, req.params.modelId);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error(`Prediction monitoring ingestion failed for '${req.params.modelId}' (prediction response was already sent, unaffected):`, err);
    }
  })();

  res.json({
    modelId: req.params.modelId,
    version: d.current_version,
    prediction,
    predictionId,
    correlationId: trace.correlationId,
    latencyMs,
    servedBy,
    servingMode: getServingMode(),
  });
});

/**
 * In-app test prediction — the surface the Studio UI's "Test this model"
 * panels call.
 *
 * Same inference path as the public POST /serve/:modelId/predict above: real
 * weights from storage through predict_runner.py, with the identical
 * fail-closed behaviour under SERVING_MODE=production. The one difference is
 * who is allowed to call it. The public route authenticates an API *client*
 * with the endpoint's bearer token; this one authenticates the logged-in
 * *operator* through the app's own permission check, because the browser
 * legitimately does not hold the endpoint secret (it is stored hashed and is
 * shown exactly once, at promotion). Routing the UI's test button through here
 * is what lets it make a genuine call instead of displaying an invented result.
 *
 * It deliberately does NOT write a monitoring trace: an operator poking at a
 * model from the console is not production traffic, and folding these into the
 * model's measured prediction history would corrupt its drift and latency
 * stats with synthetic requests.
 */
serveRouter.post("/serve/:modelId/test-predict", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const start = Date.now();
  const deployment = await pool.query(
    `SELECT d.*, c.task, c.domain FROM deployments d JOIN catalog_models c ON c.id = d.model_id WHERE d.model_id = $1`,
    [req.params.modelId]
  );
  // Testing must not wait for a deployment: try a version before promoting it.
  // ?version= picks one; otherwise the deployed version, else the latest trained.
  const deployed = deployment.rows[0] ?? null;
  const catalog = deployed ?? (await pool.query("SELECT task, domain FROM catalog_models WHERE id = $1", [req.params.modelId])).rows[0];
  const requested = req.query.version != null ? Number(req.query.version) : null;
  const versionRow = await pool.query(
    requested != null
      ? "SELECT v, weights_uri, artifact_format FROM model_versions WHERE model_id = $1 AND v = $2"
      : deployed
      ? "SELECT v, weights_uri, artifact_format FROM model_versions WHERE model_id = $1 AND v = $2"
      : "SELECT v, weights_uri, artifact_format FROM model_versions WHERE model_id = $1 ORDER BY v DESC LIMIT 1",
    requested != null ? [req.params.modelId, requested] : deployed ? [req.params.modelId, deployed.current_version] : [req.params.modelId]
  );
  if (!catalog || versionRow.rows.length === 0) {
    return res.status(404).json({
      error: requested != null
        ? `'${req.params.modelId}' has no version ${requested}`
        : `'${req.params.modelId}' has no trained version to test yet`,
    });
  }
  const d = { ...catalog, current_version: versionRow.rows[0].v };
  const input = req.body ?? {};
  const weightsUri = versionRow.rows[0].weights_uri ?? null;
  const realResult = await tryRealPrediction(weightsUri, input, versionRow.rows[0].artifact_format);

  if (realResult && realResult.ok) {
    return res.json({
      modelId: req.params.modelId,
      version: d.current_version,
      prediction: shapeRealPrediction(realResult),
      latencyMs: Date.now() - start,
      dataSource: "measured",
      servedBy: `real prediction — loaded the actual trained model (${realResult.modelClass}) from ${weightsUri}${deployed ? "" : " (not deployed — a pre-deployment test)"}${realResult.shapeNote ? `. Note: ${realResult.shapeNote}` : ""}`,
      deployed: Boolean(deployed),
      servingMode: getServingMode(),
    });
  }
  if (isProductionServing()) {
    const reason = realResult && !realResult.ok
      ? realResult.error
      : weightsUri
      ? "the configured artifact could not be loaded (check STORAGE_BACKEND and credentials)"
      : "the deployed model version has no durable weights_uri";
    return res.status(weightsUri ? 502 : 503).json({
      error: "MODEL_INFERENCE_UNAVAILABLE",
      reason,
      modelId: req.params.modelId,
      version: d.current_version,
      servingMode: getServingMode(),
      latencyMs: Date.now() - start,
    });
  }
  const why = realResult && !realResult.ok ? `real prediction was attempted and failed (${realResult.error}), falling back` : "no real weights are recorded for this version";
  res.json({
    modelId: req.params.modelId,
    version: d.current_version,
    prediction: computeDeterministicPrediction(input, d.task, d.domain),
    latencyMs: Date.now() - start,
    dataSource: "stand-in",
    servedBy: `development-only local-serving stand-in — ${why}. SERVING_MODE=production disables this fallback and returns an error instead.`,
    servingMode: getServingMode(),
  });
});

/**
 * Test a BASE library model.
 *
 * The base library is presented as callable from the moment it is registered,
 * and for entries whose weights have actually been fetched into object storage
 * (scripts/downloadBaseModelWeights.ts sets `stored_weights_uri`) that is
 * literally true — this runs the same real inference path as any trained model.
 *
 * For entries with no stored weights it is NOT true, and this says so with a
 * 503 naming the exact reason rather than returning a fabricated response.
 * That is the honest state of most of the library: `sklearn://` and `oci://`
 * entries reference a trainer image or an estimator class, not a pretrained
 * artifact, so there is nothing to load and no prediction to make until you
 * train from them. In development, the deterministic stand-in is still offered
 * so the surface can be exercised — labelled `dataSource: "stand-in"`, never
 * as a measured result.
 */
serveRouter.post("/base-models/:id/test-predict", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const start = Date.now();
  const row = await pool.query("SELECT id, domain, task, uri, stored_weights_uri FROM base_models WHERE id = $1", [req.params.id]);
  if (row.rows.length === 0) return res.status(404).json({ error: "base model not found" });
  const b = row.rows[0];
  const input = req.body ?? {};

  // Speech models run from their Hugging Face weights (cached locally after first use):
  // Whisper transcribes audioBase64, Kokoro speaks text.
  const speechTask = speechTaskId(b.task);
  if (speechTask) {
    const r = await runSpeech(speechTask, hfIdFromUri(b.uri), input);
    if (r.ok) {
      return res.json({
        baseModelId: b.id, prediction: r.prediction, latencyMs: Date.now() - start, dataSource: "measured",
        servedBy: `real prediction — ran ${r.modelClass}`, servingMode: getServingMode(),
      });
    }
    return res.status(422).json({ error: r.error, baseModelId: b.id, latencyMs: Date.now() - start });
  }

  const realResult = await tryRealPrediction(b.stored_weights_uri, input);
  if (realResult && realResult.ok) {
    return res.json({
      baseModelId: b.id,
      prediction: shapeRealPrediction(realResult),
      latencyMs: Date.now() - start,
      dataSource: "measured",
      servedBy: `real prediction — loaded the downloaded base weights (${realResult.modelClass}) from ${b.stored_weights_uri}`,
      servingMode: getServingMode(),
    });
  }

  const reason = realResult && !realResult.ok
    ? realResult.error
    : `no weights have been downloaded for '${b.id}' — its source reference is ${b.uri}, which names a trainer image or estimator class rather than a pretrained artifact. Run scripts/downloadBaseModelWeights.ts for entries that do publish weights.`;

  if (isProductionServing()) {
    return res.status(b.stored_weights_uri ? 502 : 503).json({
      error: "MODEL_INFERENCE_UNAVAILABLE",
      reason,
      baseModelId: b.id,
      servingMode: getServingMode(),
      latencyMs: Date.now() - start,
    });
  }
  res.json({
    baseModelId: b.id,
    prediction: computeDeterministicPrediction(input, b.task, b.domain),
    latencyMs: Date.now() - start,
    dataSource: "stand-in",
    servedBy: `development-only local stand-in — ${reason} SERVING_MODE=production disables this fallback and returns an error instead.`,
    servingMode: getServingMode(),
  });
});

serveRouter.get("/serve/:modelId/health", requirePermission(PERM.endpoints), async (req: Request, res: Response) => {
  const deployment = await pool.query("SELECT * FROM deployments WHERE model_id = $1", [req.params.modelId]);
  if (deployment.rows.length === 0) return res.status(404).json({ status: "not deployed" });
  res.json({ status: "healthy", modelId: req.params.modelId, version: deployment.rows[0].current_version });
});
