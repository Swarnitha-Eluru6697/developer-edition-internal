import { Router, Request, Response } from "express";
import { z } from "zod";
import { randomBytes } from "crypto";
import { pool } from "../db/connection";
import { PERM, requireAnyPermission, requirePermission } from "../middleware/permissions";
import { logAudit } from "../utils/audit";
import { hashSecret } from "../utils/secretHash";

export const endpointsRouter = Router();

function toEndpointDto(row: any) {
  return {
    modelId: row.model_id,
    title: row.title,
    url: row.url,
    status: row.status,
    authMethod: "OAuth 2.0 (Client Credentials)" as const,
    tokenEndpoint: row.token_endpoint,
    clientId: row.client_id,
    clientSecretMasked: row.client_secret_masked,
    rateLimits: row.rpm == null ? null : { rpm: row.rpm, burst: row.burst, concurrent: row.concurrent },
  };
}

/**
 * Measured serving traffic per endpoint, computed from the prediction traces
 * the serve route actually writes (model_monitoring.predictions.traces).
 *
 * Everything here is observed: request count and p95 latency over the window,
 * and when the last request arrived. Nothing is modelled or extrapolated — an
 * endpoint that has served nothing comes back with `requests: 0` and null
 * latency, and the UI says "no traffic yet" rather than showing a plausible
 * req/min figure for a model nobody has called.
 *
 * Note this reads a capped in-column trace array (the most recent 50 per
 * model), not a time-series store: over a window longer than those 50 requests
 * span, the count is a floor rather than a total. `traceCapped` says so
 * explicitly rather than letting the number be read as complete.
 */
async function endpointTraffic(windowMinutes: number) {
  const result = await pool.query(
    `SELECT model_id, predictions->'traces' AS traces FROM model_monitoring`
  );
  const cutoff = Date.now() - windowMinutes * 60_000;
  const byModel = new Map<string, any>();
  for (const row of result.rows) {
    const traces: any[] = Array.isArray(row.traces) ? row.traces : [];
    const recent = traces.filter((t) => {
      const ts = Date.parse(t?.timestamp ?? "");
      return Number.isFinite(ts) && ts >= cutoff;
    });
    const latencies = recent.map((t) => Number(t.latencyMs)).filter((n) => Number.isFinite(n)).sort((a, b) => a - b);
    const p95 = latencies.length ? latencies[Math.min(latencies.length - 1, Math.floor(latencies.length * 0.95))] : null;
    const lastTs = traces.length ? traces[traces.length - 1]?.timestamp ?? null : null;
    byModel.set(row.model_id, {
      windowMinutes,
      requests: recent.length,
      requestsPerMinute: Math.round((recent.length / windowMinutes) * 10) / 10,
      p95LatencyMs: p95,
      lastRequestAt: lastTs,
      // The trace column keeps only the most recent 50 requests per model, so a
      // full array means older requests inside this window were evicted.
      traceCapped: traces.length >= 50,
    });
  }
  return byModel;
}

endpointsRouter.get("/endpoints", requireAnyPermission(PERM.endpoints, PERM.dashboard, PERM.modelCatalog), async (req: Request, res: Response) => {
  const windowMinutes = Math.min(Math.max(Number(req.query.windowMinutes) || 60, 1), 1440);
  const result = await pool.query("SELECT * FROM endpoints ORDER BY model_id");
  const traffic = await endpointTraffic(windowMinutes);
  res.json(
    result.rows.map((row) => ({
      ...toEndpointDto(row),
      traffic: traffic.get(row.model_id) ?? {
        windowMinutes, requests: 0, requestsPerMinute: 0,
        p95LatencyMs: null, lastRequestAt: null, traceCapped: false,
      },
    }))
  );
});

endpointsRouter.get("/endpoints/:modelId", requireAnyPermission(PERM.endpoints, PERM.deploy), async (req: Request, res: Response) => {
  const windowMinutes = Math.min(Math.max(Number(req.query.windowMinutes) || 60, 1), 1440);
  const result = await pool.query("SELECT * FROM endpoints WHERE model_id = $1", [req.params.modelId]);
  if (result.rows.length === 0) return res.status(404).json({ error: "endpoint not found" });
  const traffic = await endpointTraffic(windowMinutes);
  res.json({
    ...toEndpointDto(result.rows[0]),
    traffic: traffic.get(req.params.modelId) ?? {
      windowMinutes, requests: 0, requestsPerMinute: 0,
      p95LatencyMs: null, lastRequestAt: null, traceCapped: false,
    },
  });
});

const rateLimitsSchema = z.object({
  rpm: z.number().int().positive(),
  burst: z.number().int().positive(),
  concurrent: z.number().int().positive(),
});

// PATCH /api/endpoints/:modelId/rate-limits — rejects models with no rate-limit
// pool of their own (e.g. a shared adapter), same behavior as the mockup.
endpointsRouter.patch("/endpoints/:modelId/rate-limits", requirePermission(PERM.deploy), async (req: Request, res: Response) => {
  const parsed = rateLimitsSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({ error: "invalid rate limits", details: parsed.error.flatten() });
  }
  const existing = await pool.query("SELECT * FROM endpoints WHERE model_id = $1", [req.params.modelId]);
  if (existing.rows.length === 0) return res.status(404).json({ error: "endpoint not found" });
  if (existing.rows[0].rpm == null) {
    return res.status(409).json({ error: "this endpoint shares its base model's rate limit pool — nothing separate to set" });
  }
  const updated = await pool.query(
    "UPDATE endpoints SET rpm = $1, burst = $2, concurrent = $3 WHERE model_id = $4 RETURNING *",
    [parsed.data.rpm, parsed.data.burst, parsed.data.concurrent, req.params.modelId]
  );
  res.json(toEndpointDto(updated.rows[0]));
});

// POST /api/endpoints/:modelId/regenerate-secret — returns the full secret
// exactly once in the response body; only the masked form is ever persisted.
endpointsRouter.post("/endpoints/:modelId/regenerate-secret", requirePermission(PERM.deploy), async (req: Request, res: Response) => {
  const existing = await pool.query("SELECT * FROM endpoints WHERE model_id = $1", [req.params.modelId]);
  if (existing.rows.length === 0) return res.status(404).json({ error: "endpoint not found" });

  // Math.random() is not cryptographically secure — predictable given
  // enough samples, and Node's own docs explicitly warn against using it
  // for anything security-sensitive. crypto.randomBytes() is the correct
  // primitive for a credential like this.
  const rand = randomBytes(4).toString("hex");
  const fullSecret = `cs_live_${rand}${randomBytes(8).toString("hex")}`;
  const masked = `cs_live_••••••••••••${rand}`;
  // Previously only client_secret_masked (the display value) was
  // persisted — the real client_secret column was never updated, so the
  // "new" secret handed back here would never actually have worked for
  // auth, while the OLD one silently kept working. Now that /predict and
  // /oauth/token genuinely check client_secret, that gap is consequential,
  // not cosmetic.
  await pool.query("UPDATE endpoints SET client_secret_masked = $1, client_secret_hash = $2 WHERE model_id = $3", [masked, hashSecret(fullSecret), req.params.modelId]);
  await logAudit(req.actor, "regenerated client secret", req.params.modelId);
  res.json({ clientSecret: fullSecret, clientSecretMasked: masked });
});
