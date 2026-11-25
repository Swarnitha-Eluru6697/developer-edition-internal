/*
 * FE-04: helpers for the live (no-mock) end-to-end suite. The browser drives the UI; THESE helpers are how a test independently looks at the real
 * system underneath — the source database, Kafka and Kafka Connect — so a green UI is checked against what actually happened, never against the UI itself.
 *
 * The stack is the Docker Compose project in Pralai_DataConnect_Backend/infra (default project name "infra"). Everything is overridable:
 *   DATACONNECT_COMPOSE_PROJECT   container name prefix (default "infra")
 *   DATACONNECT_API               backend base URL      (default http://localhost:8001/api/v1/dataconnect)
 *   DATACONNECT_CONNECT_URL       Kafka Connect REST    (default http://localhost:8083)
 */
import { execFileSync } from "node:child_process";

export const PROJECT = process.env.DATACONNECT_COMPOSE_PROJECT || "infra";
export const API = process.env.DATACONNECT_API || "http://localhost:8001/api/v1/dataconnect";
export const CONNECT = process.env.DATACONNECT_CONNECT_URL || "http://localhost:8083";

/** The tenant every live test works in. Its data is created and purged by the suite and never mixes with a developer's own org. */
export const ORG = "org_live_e2e";
/** The throwaway source system the connector reads: a database and a login on the stack's own PostgreSQL. */
export const SOURCE = { db: "live_e2e_src", user: "live_e2e", password: "live_e2e_pw", table: "live_orders", host: "postgres", port: 5432 };

const container = (service: string) => `${PROJECT}-${service}-1`;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function docker(args: string[], opts: { input?: string; allowFailure?: boolean; timeoutMs?: number } = {}): string {
  try {
    return execFileSync("docker", args, { encoding: "utf8", input: opts.input, timeout: opts.timeoutMs ?? 60_000, stdio: ["pipe", "pipe", "pipe"] });
  } catch (e) {
    if (opts.allowFailure) return "";
    const err = e as { stderr?: string; message: string };
    throw new Error(`docker ${args.slice(0, 4).join(" ")} failed: ${(err.stderr || err.message).toString().slice(0, 600)}`, { cause: e });
  }
}

/** Run SQL on the stack's PostgreSQL as the superuser. Returns tuples-only output. */
export function psql(sql: string, db = SOURCE.db, opts: { allowFailure?: boolean } = {}): string {
  return docker(["exec", "-i", container("postgres"), "psql", "-U", "dataconnect", "-d", db, "-v", "ON_ERROR_STOP=1", "-At"], { input: sql, ...opts }).trim();
}

/** The records currently on a topic, parsed from JSON (one per message), read from the beginning by a throwaway consumer. */
export function topicRecords(topic: string, opts: { max?: number; timeoutMs?: number } = {}): Record<string, unknown>[] {
  const out = docker([
    "exec", container("kafka"), "kafka-console-consumer", "--bootstrap-server", "localhost:9092", "--topic", topic,
    "--from-beginning", "--max-messages", String(opts.max ?? 1000), "--timeout-ms", String(opts.timeoutMs ?? 6000),
  ], { allowFailure: true, timeoutMs: (opts.timeoutMs ?? 6000) + 30_000 });
  return out.split("\n").map((l) => l.trim()).filter((l) => l.startsWith("{")).flatMap((l) => { try { return [JSON.parse(l) as Record<string, unknown>]; } catch { return []; } });
}

export function deleteTopicsWithPrefix(prefix: string): string[] {
  const all = docker(["exec", container("kafka"), "kafka-topics", "--bootstrap-server", "localhost:9092", "--list"], { allowFailure: true }).split("\n").map((t) => t.trim());
  const mine = all.filter((t) => t.startsWith(prefix));
  for (const t of mine) docker(["exec", container("kafka"), "kafka-topics", "--bootstrap-server", "localhost:9092", "--delete", "--topic", t], { allowFailure: true });
  return mine;
}

export interface ConnectStatus { connector: { state: string }; tasks: { id: number; state: string; trace?: string }[] }
export async function connectStatus(name: string): Promise<ConnectStatus | null> {
  const r = await fetch(`${CONNECT}/connectors/${encodeURIComponent(name)}/status`);
  return r.status === 404 ? null : ((await r.json()) as ConnectStatus);
}
export async function connectorNames(): Promise<string[]> {
  return (await (await fetch(`${CONNECT}/connectors`)).json()) as string[];
}

/** Call the real backend as the Admin of the live test org (used only for setup/cleanup — the workflow itself goes through the UI). */
export async function api<T = unknown>(method: string, path: string, body?: unknown): Promise<{ status: number; body: T }> {
  const r = await fetch(`${API}${path}`, {
    method,
    headers: { "Content-Type": "application/json", "X-User-Role": "DataConnect Admin", "X-Org-Id": ORG, "X-Actor": "live-e2e" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  return { status: r.status, body: (text ? JSON.parse(text) : null) as T };
}

export async function waitFor<T>(what: string, probe: () => Promise<T | false | null | undefined> | T | false | null | undefined, opts: { timeoutMs?: number; intervalMs?: number } = {}): Promise<T> {
  const deadline = Date.now() + (opts.timeoutMs ?? 90_000);
  let last: unknown;
  while (Date.now() < deadline) {
    try { const v = await probe(); if (v) return v; } catch (e) { last = e; }
    await sleep(opts.intervalMs ?? 1500);
  }
  throw new Error(`Timed out waiting for ${what}${last ? ` (last error: ${String(last).slice(0, 300)})` : ""}`);
}

/** Fail fast, with the fix, if the stack the suite needs is not up. */
export async function requireStack(): Promise<void> {
  const problems: string[] = [];
  const check = async (name: string, url: string) => { try { const r = await fetch(url); if (!r.ok) problems.push(`${name} answered ${r.status} at ${url}`); } catch { problems.push(`${name} is not reachable at ${url}`); } };
  await check("backend", `${API}/health/ready`);
  await check("Kafka Connect", `${CONNECT}/connectors`);
  try { psql("select 1", "postgres"); } catch (e) { problems.push(`PostgreSQL container ${container("postgres")} is not usable (${String(e).slice(0, 120)})`); }
  try { docker(["exec", container("kafka"), "kafka-topics", "--bootstrap-server", "localhost:9092", "--list"]); } catch { problems.push(`Kafka container ${container("kafka")} is not usable`); }
  if (problems.length) {
    throw new Error(`The live stack is not ready:\n - ${problems.join("\n - ")}\nStart it with: scripts/live-e2e.sh   (or: cd ../Pralai_DataConnect_Backend/infra && docker compose up -d)`);
  }
}

// ── The throwaway source system ────────────────────────────────────────────────────────────────────────────────────────

export function createSource(): void {
  dropSource();
  psql(`CREATE ROLE ${SOURCE.user} LOGIN PASSWORD '${SOURCE.password}'`, "postgres");
  psql(`CREATE DATABASE ${SOURCE.db} OWNER ${SOURCE.user}`, "postgres");
  psql(`
    CREATE TABLE ${SOURCE.table} (id SERIAL PRIMARY KEY, customer TEXT NOT NULL, amount NUMERIC(10,2) NOT NULL);
    INSERT INTO ${SOURCE.table} (customer, amount) VALUES ('alice', 120.50), ('bob', 75.00), ('carol', 999.99), ('dave', 5000.00), ('erin', 42.00), ('frank', -3.00);
    ALTER TABLE ${SOURCE.table} OWNER TO ${SOURCE.user};
  `);
}

export function dropSource(): void {
  psql(`SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${SOURCE.db}'`, "postgres", { allowFailure: true });
  psql(`DROP DATABASE IF EXISTS ${SOURCE.db}`, "postgres", { allowFailure: true });
  psql(`DROP ROLE IF EXISTS ${SOURCE.user}`, "postgres", { allowFailure: true });
}

/** Take the source away from the connector for real: the login is disabled and every open session killed. */
export function breakSource(): void {
  psql(`ALTER ROLE ${SOURCE.user} NOLOGIN`, "postgres");
  psql(`SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '${SOURCE.user}'`, "postgres");
}
export function repairSource(): void {
  psql(`ALTER ROLE ${SOURCE.user} LOGIN`, "postgres");
}

// ── Leaving nothing behind ─────────────────────────────────────────────────────────────────────────────────────────────

/** Remove everything the live org owns: its connectors, topics and rows (children before parents, whatever the foreign keys say). */
export async function purgeOrg(): Promise<void> {
  const pipelines = await api<{ id: string }[]>("GET", "/pipelines");
  if (pipelines.status === 200) for (const p of pipelines.body) await api("DELETE", `/pipelines/${p.id}`);
  // The backend stops a deleted pipeline's quality worker on its next reconcile (every 10s). Until then the worker can still re-create the topics
  // below, so wait it out before removing them.
  if (pipelines.status === 200 && pipelines.body.length) await sleep(13_000);
  for (const name of await connectorNames()) {
    if (name.startsWith(`${ORG}--`)) await fetch(`${CONNECT}/connectors/${encodeURIComponent(name)}`, { method: "DELETE" });
  }
  deleteTopicsWithPrefix(`${ORG}-`);
  const tables = psql(`SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='org_id' AND table_name <> 'dc_organizations'`, "dataconnect").split("\n").filter(Boolean);
  for (let pass = 0; pass < 6; pass++) {
    for (const t of tables) psql(`DELETE FROM ${t} WHERE org_id = '${ORG}'`, "dataconnect", { allowFailure: true });
  }
}

export function ensureOrg(): void {
  psql(`INSERT INTO dc_organizations (org_id, name, status) VALUES ('${ORG}', 'Live E2E', 'active') ON CONFLICT (org_id) DO NOTHING`, "dataconnect");
}
