import express from "express";
import cors from "cors";
import { gatewayAuth, meBody } from "@aadhyaanalyticsinc/gateway-auth";
import { pool } from "./db/connection";
import { actorMiddleware, PERM, requireAnyPermission, requirePermission } from "./middleware/permissions";
import { artifactsRouter } from "./routes/artifacts";
import { modelsRouter } from "./routes/models";
import { datasetsRouter } from "./routes/datasets";
import { endpointsRouter } from "./routes/endpoints";
import { alertsRouter } from "./routes/alerts";
import { trainingRunsRouter } from "./routes/trainingRuns";
import { pipelinesRouter } from "./routes/pipelines";
import { deployRouter } from "./routes/deploy";
import { monitoringRouter } from "./routes/monitoring";
import { clustersRouter } from "./routes/clusters";
import { dashboardRouter } from "./routes/dashboard";
import { experimentsRouter } from "./routes/experiments";
import { hpoRouter } from "./routes/hpo";
import { serveRouter } from "./routes/serve";
import { oauthRouter } from "./routes/oauth";
import { ucsHealthy } from "./ucs/client";
import { ucsConfig } from "./ucs/config";

// The Express app, without listening or background loops (server.ts does
// those), so tests can drive the real middleware and routers.

const app = express();
app.use(cors());
// Deny by default (contract 5.1), before any body is parsed: every route below
// needs a request that came through Envoy (TRUST_ENVOY_HEADERS=true + a matching x-inaiera-gateway-secret)
// and carries an identity; otherwise 401. The only public route is the health
// probe. Each route then declares the permission it needs (403 without it).
// Identity is read only from Envoy's headers — never x-user-role.
app.use(gatewayAuth({ publicPaths: ["/api/health"] }));
app.use(actorMiddleware);

// The dataset upload streams the raw request body to disk itself. Letting the
// JSON parser run first would consume a .json upload's body, and the route
// would then wait forever for bytes that already went — the upload hangs and
// the browser sees a proxy 500 (CSV was unaffected, which hid it).
// Speech-to-text tests send audio as base64 inside JSON (a 2-minute WAV is ~7 MB),
// well past express.json's 100 KB default. Configurable via JSON_BODY_LIMIT.
const jsonParser = express.json({ limit: process.env.JSON_BODY_LIMIT || "30mb" });
const RAW_UPLOAD_PATH = /^\/api\/datasets\/[^/]+\/upload\/?$/;
app.use((req, res, next) => (RAW_UPLOAD_PATH.test(req.path) ? next() : jsonParser(req, res, next)));

app.get("/api/health", (_req, res) => res.json({ ok: true }));
// The caller and what they may do — the UI shows pages from `permissions`.
// Any verified caller may ask, including one holding no permissions.
app.get(["/api/me", "/api/whoami"], (req, res) => res.json(meBody(req.principal!)));

// Real fleet-wide worker visibility — not this process's own status
// (which separate worker processes don't share memory with anyway),
// but a real query against the same real Postgres queue every worker
// process claims from: which real workers (by their real hostname+PID
// identity) currently hold which real in-flight runs, and how many
// real queued runs are still waiting. Genuinely useful for confirming
// a real multi-process worker fleet is actually sharing the load, using
// only data that already exists — no new tracking infrastructure.
app.get("/api/workers", requireAnyPermission(PERM.pipelines, PERM.clusters), async (_req, res) => {
  const inFlight = await pool.query(
    `SELECT claimed_by, COUNT(*) AS run_count, array_agg(id) AS run_ids, MAX(heartbeat_at) AS last_heartbeat
     FROM training_runs WHERE status = 'running' AND claimed_by IS NOT NULL GROUP BY claimed_by ORDER BY claimed_by`
  );
  const queued = await pool.query(`SELECT COUNT(*) AS c FROM training_runs WHERE status = 'queued'`);
  const hpoInFlight = await pool.query(
    `SELECT claimed_by, COUNT(*) AS trial_count, array_agg(id) AS trial_ids, MAX(heartbeat_at) AS last_heartbeat
     FROM hpo_trials WHERE status='running' AND claimed_by IS NOT NULL GROUP BY claimed_by ORDER BY claimed_by`
  );
  const hpoQueued = await pool.query(`SELECT COUNT(*) AS c FROM hpo_trials WHERE status='queued'`);
  // Every worker loop beats into worker_heartbeats each poll cycle, so idle
  // workers are listed too. A process runs a training loop and an HPO loop;
  // they're shown as one worker (one row per hostname+pid), capacity summed.
  const beats = await pool.query(
    `SELECT *, EXTRACT(EPOCH FROM (NOW() - last_seen))::int AS secs FROM worker_heartbeats
      WHERE last_seen > NOW() - INTERVAL '120 seconds' ORDER BY hostname, pid, role`
  ).catch(() => ({ rows: [] as any[] }));
  const runNames = await pool.query(
    `SELECT claimed_by, id, model_name FROM training_runs WHERE status='running' AND claimed_by IS NOT NULL ORDER BY claimed_at`
  );
  const trialNames = await pool.query(
    `SELECT t.claimed_by, t.id, t.trial_number, s.num_trials, s.base_model_id, s.dataset_name
       FROM hpo_trials t JOIN hpo_searches s ON s.id = t.search_id
      WHERE t.status='running' AND t.claimed_by IS NOT NULL ORDER BY t.claimed_at`
  );
  const LIVE_WITHIN_SECONDS = 30; // loops beat every ~2s; 30s of silence means the process is gone or wedged
  const fleetMap = new Map<string, any>();
  for (const b of beats.rows) {
    const key = `${b.hostname}:${b.pid}`;
    const w = fleetMap.get(key) ?? { workerId: b.worker_id, hostname: b.hostname, pid: b.pid, pools: new Set<string>(), roles: [] as string[], capacity: 0, inFlight: 0, secondsSinceHeartbeat: b.secs, ids: [] as string[], current: [] as any[] };
    if (b.role === "training") w.workerId = b.worker_id; // the training loop's id is the one shown on runs
    w.ids.push(b.worker_id);
    w.pools.add(b.pool);
    w.roles.push(b.role);
    w.capacity += b.concurrency;
    w.inFlight += b.in_flight;
    w.secondsSinceHeartbeat = Math.min(w.secondsSinceHeartbeat, b.secs);
    fleetMap.set(key, w);
  }
  const byWorkerId = new Map<string, any>();
  for (const w of fleetMap.values()) for (const id of w.ids) byWorkerId.set(id, w);
  for (const r of runNames.rows) byWorkerId.get(r.claimed_by)?.current.push({ kind: "training-run", id: r.id, label: r.model_name });
  for (const t of trialNames.rows) byWorkerId.get(t.claimed_by)?.current.push({ kind: "hpo-trial", id: t.id, label: `tune-${t.base_model_id}-${t.dataset_name} · trial ${t.trial_number}/${t.num_trials}` });
  const fleet = [...fleetMap.values()].map(({ pools, ids, ...w }) => {
    const p = [...pools];
    // In flight counts the rows it actually holds right now — exact, where the
    // heartbeat's own count can lag by one poll cycle.
    return { ...w, inFlight: w.current.length, pool: p.length === 1 ? p[0] : p.includes("gpu") ? "gpu" : "any", live: w.secondsSinceHeartbeat <= LIVE_WITHIN_SECONDS };
  });
  // Liveness is the freshness of the heartbeat a worker writes on the row it
  // holds, measured against the SAME threshold the workers' own reaper uses to
  // declare a run abandoned — so what this reports as stale is exactly what the
  // system will act on, not a second opinion.
  const STALE_AFTER_SECONDS = 120;
  const now = Date.now();
  const sinceBeat = (hb: any) => (hb ? Math.round((now - new Date(hb).getTime()) / 1000) : null);
  res.json({
    measuredFrom: "queue state — claimed_by / heartbeat_at on training_runs and hpo_trials",
    // An idle worker holds no row, so it genuinely cannot appear here. Stated
    // outright so an empty list isn't misread as "no workers are running".
    note: "Only workers currently holding a claimed row are visible; an idle worker process touches no row and is undetectable from queue state alone.",
    // Every running worker process, idle ones included, from worker_heartbeats.
    fleet,
    liveWithinSeconds: LIVE_WITHIN_SECONDS,
    staleAfterSeconds: STALE_AFTER_SECONDS,
    workers: inFlight.rows.map((r) => ({
      workerId: r.claimed_by, inFlightRuns: Number(r.run_count), runIds: r.run_ids,
      lastHeartbeat: r.last_heartbeat,
      secondsSinceHeartbeat: sinceBeat(r.last_heartbeat),
      stale: (sinceBeat(r.last_heartbeat) ?? 0) > STALE_AFTER_SECONDS,
    })),
    queuedRuns: Number(queued.rows[0].c),
    hpoWorkers: hpoInFlight.rows.map((r) => ({
      workerId: r.claimed_by, inFlightTrials: Number(r.trial_count), trialIds: r.trial_ids,
      lastHeartbeat: r.last_heartbeat,
      secondsSinceHeartbeat: sinceBeat(r.last_heartbeat),
      stale: (sinceBeat(r.last_heartbeat) ?? 0) > STALE_AFTER_SECONDS,
    })),
    queuedHpoTrials: Number(hpoQueued.rows[0].c),
  });
});

// GET /api/ucs/status — is the real Unified Catalog Service reachable.
// Useful for verifying the integration is actually connected, not just
// configured. RBAC decisions now go through UCS's real authz/check
// per-action (middleware/permissions.ts) rather than a locally-cached matrix, so
// there's no local matrix state to report here anymore.
app.get("/api/ucs/status", requirePermission(PERM.access), async (_req, res) => {
  const healthy = await ucsHealthy();
  res.json({ baseUrl: ucsConfig.baseUrl, modelCatalog: ucsConfig.modelCatalog, healthy });
});

app.use("/api", modelsRouter);
app.use("/api", artifactsRouter);
app.use("/api", datasetsRouter);
app.use("/api", endpointsRouter);
app.use("/api", alertsRouter);
app.use("/api", trainingRunsRouter);
app.use("/api", pipelinesRouter);
app.use("/api", deployRouter);
app.use("/api", monitoringRouter);
app.use("/api", clustersRouter);
app.use("/api", dashboardRouter);
app.use("/api", experimentsRouter);
app.use("/api", hpoRouter);
app.use("/api", serveRouter);
app.use(oauthRouter);

app.use((err: any, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
  // eslint-disable-next-line no-console
  console.error(err);

  // A client error already knows its own status and has a reason worth
  // reading. Flattening everything to 500 "internal server error" turned
  // "your file is 332 MB and the limit is 50 MB" — which the caller can act
  // on — into "something broke", which they cannot. Middleware that throws
  // this way (body-parser's PayloadTooLargeError, malformed-JSON
  // SyntaxError) sets `status`/`statusCode`; only 4xx is echoed back.
  //
  // 5xx stays deliberately opaque: an unexpected server fault can carry
  // connection strings, file paths and stack detail, and none of that
  // belongs in a response body. It is logged above instead.
  const status = Number(err?.status ?? err?.statusCode);
  if (Number.isInteger(status) && status >= 400 && status < 500) {
    return res.status(status).json({
      error: typeof err?.message === "string" && err.message ? err.message : "bad request",
      ...(err?.type === "entity.too.large" && err.limit
        ? { limitBytes: err.limit, receivedBytes: err.length ?? null }
        : {}),
    });
  }
  res.status(500).json({ error: "internal server error" });
});

export { app };
