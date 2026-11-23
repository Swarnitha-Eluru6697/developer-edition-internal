import { Router, Request, Response } from "express";
import { z } from "zod";
import { pool } from "../db/connection";
import { PERM, requireAnyPermission, requirePermission } from "../middleware/permissions";
import { recomputeModelHealth } from "../services/modelHealth";

export const alertsRouter = Router();

const WINDOWS_TO_FIRE = 3;

// v2.36.8: every seeded policy is evaluated from measured production
// signals. Policies are skipped when the required real signal/window is
// unavailable; no default/fabricated value is substituted.
const EVALUABLE_POLICY_IDS = new Set(["p95-latency","low-conf","f1-drop","recall-drop","psi","error-rate","missing-values","gt-coverage"]);
const MIN_SAMPLES_TO_EVALUATE = 5; // fewer real traces than this = insufficient data, not a fabricated signal

/** A user-added rule watches a built-in signal; a built-in rule is its own signal. */
const signalOf = (policy: { id: string; signal?: string | null }) => policy.signal ?? policy.id;

/** Optional ?from=YYYY-MM-DD&to=YYYY-MM-DD (both inclusive). Anything else is ignored rather than rejected. */
function dateRange(req: Request): [string | null, string | null] {
  const day = (v: unknown) => (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v) ? v : null);
  return [day(req.query.from), day(req.query.to)];
}
const inRange = (column: string, fromParam: number) =>
  `($${fromParam}::date IS NULL OR ${column} >= $${fromParam}::date) AND ($${fromParam + 1}::date IS NULL OR ${column} < $${fromParam + 1}::date + 1)`;

export function percentile(values: number[], p: number): number {
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[Math.max(0, idx)];
}

const SEVERITY_RANK: Record<string, number> = { low: 0, medium: 1, high: 2 };

async function dispatchAlertWebhook(policy: any, modelId: string, formattedValue: string): Promise<void> {
  if (!policy.channel_id) return;
  const channelRow = await pool.query("SELECT * FROM notification_channels WHERE id = $1", [policy.channel_id]);
  const channel = channelRow.rows[0];
  if (!channel || !channel.enabled || !channel.webhook_url) return;
  if (SEVERITY_RANK[policy.severity] < SEVERITY_RANK[channel.min_severity ?? "low"]) return;
  if (channel.model_id && channel.model_id !== modelId) return;

  const message = `🚨 *${policy.severity.toUpperCase()}* — ${policy.label} fired for \`${modelId}\` (current: ${formattedValue}, threshold: ${policy.operator} ${policy.default_threshold}${policy.unit})`;
  // Real, correct payload shape for each provider's actual incoming-
  // webhook format — Slack's simple {text} form, Teams' legacy
  // MessageCard form (still what classic Incoming Webhook connectors
  // accept). Previously no dispatch happened at all: channels existed
  // as pure CRUD metadata with no real webhook_url to even send to.
  const body =
    channel.type === "slack"
      ? { text: message }
      : { "@type": "MessageCard", "@context": "http://schema.org/extensions", summary: policy.label, text: message };

  try {
    const res = await fetch(channel.webhook_url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      // eslint-disable-next-line no-console
      console.error(`Alert webhook dispatch to channel '${channel.id}' failed: HTTP ${res.status}`);
    }
  } catch (err) {
    // A webhook failing to send should never break alert evaluation
    // itself — the alert state is already correctly recorded regardless.
    // eslint-disable-next-line no-console
    console.error(`Alert webhook dispatch to channel '${channel.id}' failed:`, err instanceof Error ? err.message : err);
  }
}

export async function evaluateAlerts(): Promise<{ evaluated: number; skippedInsufficientData: number; fired: string[] }> {
  const policies = await pool.query("SELECT * FROM alert_policies WHERE enabled = TRUE AND COALESCE(signal, id) = ANY($1)", [Array.from(EVALUABLE_POLICY_IDS)]);
  const deployedModels = await pool.query(
    `SELECT d.model_id, mm.model_type AS task, mm.predictions FROM deployments d
     JOIN catalog_models c ON c.id = d.model_id
     LEFT JOIN model_monitoring mm ON mm.model_id = d.model_id`
  );

  let evaluated = 0;
  let skippedInsufficientData = 0;
  const fired: string[] = [];

  for (const policy of policies.rows) {
    const appliesTo: string[] = policy.applies_to || [];
    const signal = signalOf(policy);
    for (const model of deployedModels.rows) {
      if (!appliesTo.includes(model.task)) continue;
      if (policy.model_id && policy.model_id !== model.model_id) continue;
      const traces: any[] = model.predictions?.traces ?? [];
      const recentReal = traces.filter((t) => t.dataSource === "measured").slice(-50);

      let currentValue: number | null = null;
      let unit = policy.unit;
      const health = await recomputeModelHealth(pool, model.model_id);
      const signals = health?.signals || {};
      if (signal === "p95-latency") {
        const latencies = recentReal.map((t) => t.latencyMs).filter((v) => typeof v === "number");
        if (latencies.length < MIN_SAMPLES_TO_EVALUATE) { skippedInsufficientData++; continue; }
        currentValue = percentile(latencies, 95);
      } else if (signal === "low-conf") {
        const confidences = recentReal.map(t=>t.confidence).filter(v=>typeof v === "number");
        if (confidences.length < MIN_SAMPLES_TO_EVALUATE) { skippedInsufficientData++; continue; }
        currentValue = Math.round((confidences.filter((v:number)=>v<0.6).length / confidences.length) * 1000) / 10;
      } else if (signal === "f1-drop") currentValue = signals.f1DropPct ?? null;
      else if (signal === "recall-drop") currentValue = signals.recallDropPct ?? null;
      else if (signal === "psi") currentValue = signals.maxPsi ?? null;
      else if (signal === "error-rate") currentValue = signals.errorRatePct ?? null;
      else if (signal === "missing-values") currentValue = signals.missingValuesPct ?? null;
      else if (signal === "gt-coverage") currentValue = signals.groundTruthCoveragePct ?? null;
      if (currentValue === null || !Number.isFinite(currentValue)) { skippedInsufficientData++; continue; }

      const stateRow = await pool.query("SELECT override_threshold FROM alert_state WHERE model_id = $1 AND policy_id = $2", [model.model_id, policy.id]);
      const threshold = stateRow.rows[0]?.override_threshold ?? policy.default_threshold;
      const breached = policy.operator === ">" ? currentValue > threshold : currentValue < threshold;

      const formattedValue = `${currentValue}${unit}`;
      if (breached) {
        const updated = await pool.query(
          `INSERT INTO alert_state (model_id, policy_id, windows_exceeded, current_value, ack_state)
           VALUES ($1, $2, 1, $3, 'none')
           ON CONFLICT (model_id, policy_id) DO UPDATE SET
             windows_exceeded = alert_state.windows_exceeded + 1, current_value = $3,
             -- a new breach after a clear starts a new alert; one still breaching keeps its start time
             raised_at = CASE WHEN alert_state.windows_exceeded = 0 THEN NOW() ELSE alert_state.raised_at END
           RETURNING windows_exceeded`,
          [model.model_id, policy.id, formattedValue]
        );
        if (updated.rows[0].windows_exceeded === WINDOWS_TO_FIRE) {
          fired.push(`${model.model_id}/${policy.id}`);
          await dispatchAlertWebhook(policy, model.model_id, formattedValue);
        }
      } else {
        await pool.query(
          `INSERT INTO alert_state (model_id, policy_id, windows_exceeded, current_value, ack_state)
           VALUES ($1, $2, 0, $3, 'none')
           ON CONFLICT (model_id, policy_id) DO UPDATE SET windows_exceeded = 0, current_value = $3`,
          [model.model_id, policy.id, formattedValue]
        );
      }
      evaluated++;
    }
  }
  return { evaluated, skippedInsufficientData, fired };
}

// POST /api/alerts/evaluate — runs one real evaluation pass now, for
// every deployed model against every policy whose required measured signal is available. Call this repeatedly (an operator, or point a real cron/
// scheduler at it) for real periodic evaluation.
alertsRouter.post("/alerts/evaluate", requirePermission(PERM.alertsEdit), async (_req: Request, res: Response) => {
  const result = await evaluateAlerts();
  res.json(result);
});

function policyDto(r: any) {
  return {
    id: r.id,
    name: r.name,
    label: r.label,
    severity: r.severity,
    operator: r.operator,
    unit: r.unit,
    window: r.window_duration,
    action: r.action,
    enabled: r.enabled,
    appliesTo: r.applies_to,
    defaultThreshold: r.default_threshold,
    channelId: r.channel_id,
    signal: signalOf(r),
    builtIn: r.signal == null,
    modelId: r.model_id ?? null,
    createdAt: r.created_at,
  };
}

alertsRouter.get("/alert-policies", requireAnyPermission(PERM.alertsView, PERM.modelOverview), async (req: Request, res: Response) => {
  const [from, to] = dateRange(req);
  const result = await pool.query(
    `SELECT * FROM alert_policies WHERE ${inRange("created_at", 1)} ORDER BY (signal IS NOT NULL), id`, [from, to]
  );
  // Per-model overrides already exist (alert_state.override_threshold, written
  // by the PATCH below) but were never readable, so the rule editor had no way
  // to show which models had been retuned — it could only ever write blind.
  const overrides = await pool.query(
    `SELECT policy_id, model_id, override_threshold
       FROM alert_state
      WHERE override_threshold IS NOT NULL
      ORDER BY policy_id, model_id`
  );
  // `evaluable` is the honest distinction between a rule the engine can
  // actually compute a value for and one it cannot. Toggling a rule outside
  // that set on would change nothing, and the UI needs to be able to say so.
  res.json(
    result.rows.map((r) => ({
      ...policyDto(r),
      evaluable: EVALUABLE_POLICY_IDS.has(signalOf(r)),
      overrides: overrides.rows
        .filter((o) => o.policy_id === r.id)
        .map((o) => ({ modelId: o.model_id, threshold: Number(o.override_threshold) })),
    }))
  );
});

// POST /api/alert-policies — add a rule on one of the built-in signals. The
// engine can only measure those signals, so a rule names which one it watches
// and inherits its label, operator, unit and model types; the user sets the
// name, threshold, severity, scope (one model or all) and channel.
const createPolicySchema = z.object({
  name: z.string().trim().min(1).max(80),
  signal: z.string().refine((s) => EVALUABLE_POLICY_IDS.has(s), "unknown signal"),
  severity: z.enum(["low", "medium", "high"]),
  threshold: z.number().finite(),
  modelId: z.string().min(1).nullable().default(null),
  channelId: z.string().min(1).nullable().default(null),
  enabled: z.boolean().default(true),
});
alertsRouter.post("/alert-policies", requirePermission(PERM.alertsEdit), async (req: Request, res: Response) => {
  const parsed = createPolicySchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid rule", details: parsed.error.flatten() });
  const d = parsed.data;
  const base = await pool.query("SELECT * FROM alert_policies WHERE id = $1", [d.signal]);
  if (base.rows.length === 0) return res.status(400).json({ error: `signal '${d.signal}' has no built-in rule to copy` });
  const clash = await pool.query("SELECT 1 FROM alert_policies WHERE lower(name) = lower($1)", [d.name]);
  if (clash.rows.length) return res.status(409).json({ error: `a rule named '${d.name}' already exists` });
  const b = base.rows[0];
  const created = await pool.query(
    `INSERT INTO alert_policies (id, name, label, severity, operator, unit, window_duration, action, enabled,
                                 applies_to, default_threshold, channel_id, signal, model_id)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) RETURNING *`,
    [`rule-${Date.now()}`, d.name, b.label, d.severity, b.operator, b.unit, b.window_duration, b.action, d.enabled,
     JSON.stringify(b.applies_to), d.threshold, d.channelId, d.signal, d.modelId]
  );
  res.status(201).json({ ...policyDto(created.rows[0]), evaluable: true, overrides: [] });
});

// DELETE /api/alert-policies/:id — only rules a user added. A built-in rule
// is the only definition of its signal, so it is turned off, not deleted.
alertsRouter.delete("/alert-policies/:id", requirePermission(PERM.alertsEdit), async (req: Request, res: Response) => {
  const existing = await pool.query("SELECT signal FROM alert_policies WHERE id = $1", [req.params.id]);
  if (existing.rows.length === 0) return res.status(404).json({ error: "policy not found" });
  if (existing.rows[0].signal == null) return res.status(400).json({ error: "built-in rules can't be deleted — turn the rule off instead" });
  await pool.query("DELETE FROM alert_policies WHERE id = $1", [req.params.id]);
  res.json({ ok: true });
});

// PATCH /api/alert-policies/:id — toggle enabled, change the default threshold,
// or reroute to a different notification channel. Admin/engineer only —
// mirrors the mockup's role-gated rule editor.
const patchPolicySchema = z.object({
  enabled: z.boolean().optional(),
  defaultThreshold: z.number().optional(),
  channelId: z.string().nullable().optional(),
});
alertsRouter.patch("/alert-policies/:id", requirePermission(PERM.alertsEdit), async (req: Request, res: Response) => {
  const parsed = patchPolicySchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid update", details: parsed.error.flatten() });
  const existing = await pool.query("SELECT * FROM alert_policies WHERE id = $1", [req.params.id]);
  if (existing.rows.length === 0) return res.status(404).json({ error: "policy not found" });

  const next = { ...existing.rows[0], ...parsed.data };
  const updated = await pool.query(
    `UPDATE alert_policies SET enabled = $1, default_threshold = $2, channel_id = $3 WHERE id = $4 RETURNING *`,
    [next.enabled, next.defaultThreshold ?? next.default_threshold, next.channelId ?? next.channel_id, req.params.id]
  );
  res.json(policyDto(updated.rows[0]));
});

// PATCH /api/alert-policies/:id/override/:modelId — set (or clear, with
// threshold: null) a per-model threshold override, same concept as the
// mockup's rule editor's per-model override fields.
const overrideSchema = z.object({ threshold: z.number().nullable() });
alertsRouter.patch("/alert-policies/:id/override/:modelId", requirePermission(PERM.alertsEdit), async (req: Request, res: Response) => {
  const parsed = overrideSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid override", details: parsed.error.flatten() });
  await pool.query(
    `INSERT INTO alert_state (model_id, policy_id, override_threshold)
     VALUES ($1, $2, $3)
     ON CONFLICT (model_id, policy_id) DO UPDATE SET override_threshold = $3`,
    [req.params.modelId, req.params.id, parsed.data.threshold]
  );
  res.json({ ok: true });
});

// GET /api/alerts/active — every model's currently-firing or building alert,
// joined against its policy definition, mirroring the mockup's Active tab.
alertsRouter.get("/alerts/active", requireAnyPermission(PERM.alertsView, PERM.modelOverview), async (req: Request, res: Response) => {
  const result = await pool.query(
    `SELECT s.*, p.name, p.label, p.severity, p.operator, p.unit, p.window_duration, p.action, p.default_threshold
     FROM alert_state s JOIN alert_policies p ON p.id = s.policy_id
     WHERE s.windows_exceeded > 0 AND s.ack_state != 'resolved' AND ${inRange("s.raised_at", 1)}
     ORDER BY s.windows_exceeded DESC`,
    dateRange(req)
  );
  res.json(
    result.rows.map((r) => ({
      modelId: r.model_id,
      policyId: r.policy_id,
      policyName: r.name,
      label: r.label,
      severity: r.severity,
      condition: `${r.operator} ${r.override_threshold ?? r.default_threshold}${r.unit}`,
      currentValue: r.current_value,
      windowsExceeded: r.windows_exceeded,
      windowsToFire: WINDOWS_TO_FIRE,
      status: r.windows_exceeded >= WINDOWS_TO_FIRE ? "FIRED" : "BUILDING",
      ackState: r.ack_state,
      raisedAt: r.raised_at,
    }))
  );
});

alertsRouter.post("/alerts/:modelId/:policyId/acknowledge", requirePermission(PERM.alertsEdit), async (req: Request, res: Response) => {
  // Resolve resets windows_exceeded to 0, so only an alert still over
  // threshold can be acknowledged — an Acknowledge sent after Resolve must
  // not mark the next firing as already acknowledged.
  const result = await pool.query(
    "UPDATE alert_state SET ack_state = 'acknowledged' WHERE model_id = $1 AND policy_id = $2 AND windows_exceeded > 0",
    [req.params.modelId, req.params.policyId]
  );
  if (result.rowCount === 0) {
    const exists = await pool.query("SELECT 1 FROM alert_state WHERE model_id = $1 AND policy_id = $2", [req.params.modelId, req.params.policyId]);
    if (exists.rows.length === 0) return res.status(404).json({ error: "alert not found" });
    return res.status(409).json({ error: "This alert is already resolved, so it cannot be acknowledged" });
  }
  res.json({ ok: true });
});

alertsRouter.post("/alerts/:modelId/:policyId/resolve", requirePermission(PERM.alertsEdit), async (req: Request, res: Response) => {
  const existing = await pool.query("SELECT * FROM alert_state WHERE model_id = $1 AND policy_id = $2", [req.params.modelId, req.params.policyId]);
  if (existing.rows.length === 0) return res.status(404).json({ error: "alert not found" });

  // Record the resolution permanently (History), then reset current state
  // back to 'none' rather than 'resolved' — so if the same alert genuinely
  // fires again later, it behaves like a fresh alert, not a resurrection
  // of a "resolved" one that would be filtered out of Active forever.
  await pool.query(
    `INSERT INTO alert_history (model_id, policy_id, final_value, windows_at_resolution, resolved_by)
     VALUES ($1, $2, $3, $4, $5)`,
    [req.params.modelId, req.params.policyId, existing.rows[0].current_value, existing.rows[0].windows_exceeded, req.actor]
  );
  await pool.query(
    "UPDATE alert_state SET ack_state = 'none', windows_exceeded = 0 WHERE model_id = $1 AND policy_id = $2",
    [req.params.modelId, req.params.policyId]
  );
  res.json({ ok: true });
});

// GET /api/alerts/history — every past resolution, newest first.
alertsRouter.get("/alerts/history", requireAnyPermission(PERM.alertsView, PERM.modelOverview), async (req: Request, res: Response) => {
  const result = await pool.query(
    `SELECT h.*, p.name, p.label FROM alert_history h JOIN alert_policies p ON p.id = h.policy_id
     WHERE ${inRange("h.resolved_at", 1)} ORDER BY h.resolved_at DESC LIMIT 100`,
    dateRange(req)
  );
  res.json(
    result.rows.map((r) => ({
      id: r.id,
      modelId: r.model_id,
      policyName: r.name,
      label: r.label,
      finalValue: r.final_value,
      windowsAtResolution: r.windows_at_resolution,
      resolvedBy: r.resolved_by,
      resolvedAt: r.resolved_at,
    }))
  );
});

// ---- Notification channels — full CRUD, matching the mockup's N-channel list ----

function maskWebhook(url: string): string {
  if (!url) return "";
  try {
    const u = new URL(url);
    const tail = u.pathname.split("/").filter(Boolean).pop() || "";
    return `${u.origin}/••••••••/${tail.slice(-4)}`;
  } catch {
    return url.length > 8 ? `••••••••${url.slice(-4)}` : "••••••••";
  }
}

function channelDto(r: any) {
  // webhook_url (the real, dispatchable URL) is intentionally never
  // returned — same reasoning as never returning a client_secret_hash's
  // plaintext. webhook_masked is derived server-side for display.
  return {
    id: r.id, name: r.name, type: r.type, enabled: r.enabled, webhookMasked: r.webhook_masked, channelDest: r.channel_dest,
    minSeverity: r.min_severity ?? "low", modelId: r.model_id ?? null,
  };
}

alertsRouter.get("/notification-channels", requireAnyPermission(PERM.alertsView, PERM.modelOverview), async (_req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM notification_channels ORDER BY name");
  res.json(result.rows.map(channelDto));
});

const channelSchema = z.object({
  name: z.string().min(1),
  type: z.enum(["slack", "teams"]),
  // Real, usable webhook URL — previously this field (named
  // webhookMasked) was what the CLIENT submitted directly, meaning
  // there was never a way to provide an actual dispatchable URL through
  // this API at all. webhookMasked is now always computed server-side
  // from this real value, never client-supplied.
  webhookUrl: z.string().trim().url().refine((u) => u.startsWith("https://"), "must be https://"),
  channelDest: z.string().default(""),
  minSeverity: z.enum(["low", "medium", "high"]).default("low"),
  modelId: z.string().min(1).nullable().default(null),
  enabled: z.boolean().default(true),
});
alertsRouter.post("/notification-channels", requirePermission(PERM.alertsEdit), async (req: Request, res: Response) => {
  const parsed = channelSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid channel", details: parsed.error.flatten() });
  const clash = await pool.query("SELECT 1 FROM notification_channels WHERE lower(name) = lower($1)", [parsed.data.name]);
  if (clash.rows.length) return res.status(409).json({ error: `a channel named '${parsed.data.name}' already exists` });
  const id = `ch-${Date.now()}`;
  const d = parsed.data;
  const result = await pool.query(
    `INSERT INTO notification_channels (id, name, type, enabled, webhook_url, webhook_masked, channel_dest, min_severity, model_id)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING *`,
    [id, d.name, d.type, d.enabled, d.webhookUrl, maskWebhook(d.webhookUrl), d.channelDest, d.minSeverity, d.modelId]
  );
  res.status(201).json(channelDto(result.rows[0]));
});

alertsRouter.patch("/notification-channels/:id", requirePermission(PERM.alertsEdit), async (req: Request, res: Response) => {
  // Every field optional on an edit; a blank webhookUrl keeps the stored one.
  const parsed = channelSchema.partial().extend({ webhookUrl: channelSchema.shape.webhookUrl.optional().or(z.literal("")) }).safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid update", details: parsed.error.flatten() });
  const existing = await pool.query("SELECT * FROM notification_channels WHERE id = $1", [req.params.id]);
  if (existing.rows.length === 0) return res.status(404).json({ error: "channel not found" });
  const row = existing.rows[0];
  const nextName = parsed.data.name ?? row.name;
  const nextType = parsed.data.type ?? row.type;
  const nextEnabled = parsed.data.enabled ?? row.enabled;
  const nextWebhookUrl = parsed.data.webhookUrl || row.webhook_url;
  const nextChannelDest = parsed.data.channelDest ?? row.channel_dest;
  const nextMinSeverity = parsed.data.minSeverity ?? row.min_severity;
  const nextModelId = parsed.data.modelId !== undefined ? parsed.data.modelId : row.model_id;
  if (nextName.toLowerCase() !== String(row.name).toLowerCase()) {
    const clash = await pool.query("SELECT 1 FROM notification_channels WHERE lower(name) = lower($1) AND id != $2", [nextName, req.params.id]);
    if (clash.rows.length) return res.status(409).json({ error: `a channel named '${nextName}' already exists` });
  }
  const result = await pool.query(
    `UPDATE notification_channels SET name = $1, type = $2, enabled = $3, webhook_url = $4, webhook_masked = $5, channel_dest = $6,
            min_severity = $7, model_id = $8 WHERE id = $9 RETURNING *`,
    [nextName, nextType, nextEnabled, nextWebhookUrl, maskWebhook(nextWebhookUrl), nextChannelDest, nextMinSeverity, nextModelId, req.params.id]
  );
  res.json(channelDto(result.rows[0]));
});

alertsRouter.delete("/notification-channels/:id", requirePermission(PERM.alertsEdit), async (req: Request, res: Response) => {
  // Same behavior as the mockup: deleting a channel reassigns any rules
  // routed to it, rather than leaving them pointing at nothing.
  const remaining = await pool.query("SELECT id FROM notification_channels WHERE id != $1 ORDER BY name LIMIT 1", [req.params.id]);
  const fallback = remaining.rows[0]?.id ?? null;
  await pool.query("UPDATE alert_policies SET channel_id = $1 WHERE channel_id = $2", [fallback, req.params.id]);
  const result = await pool.query("DELETE FROM notification_channels WHERE id = $1", [req.params.id]);
  if (result.rowCount === 0) return res.status(404).json({ error: "channel not found" });
  res.json({ ok: true, reassignedTo: fallback });
});
