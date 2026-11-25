import { ALL_BASE_MODELS } from "./baseModelsData";
import { ALGORITHM_SPECS } from "./algorithmSpecs";

/**
 * The reference catalog every deployment needs before anyone can train:
 * algorithm_specs, base_models and compute_pools.
 *
 * One definition of each table's columns and row values, used twice:
 *  - seed.ts (dev only) drops every table and INSERTs these rows, plus demo data.
 *  - loadReferenceData() (every server start) UPSERTs them into whatever is
 *    already there and never deletes anything.
 * Keeping both on the same column lists is the point: a column added to the
 * catalog arrives in a fresh dev seed and in a running deployment together.
 */

export interface Queryable {
  query(text: string, values?: unknown[]): Promise<{ rows: any[]; rowCount?: number | null }>;
}
export interface ConnectablePool {
  connect(): Promise<Queryable & { release(err?: boolean | Error): void }>;
}

export interface CatalogTable {
  table: string;
  /** Primary key — the conflict target of the upsert. */
  key: string;
  /** Every column the catalog owns, key first. Runtime columns (e.g.
   *  base_models.stored_weights_uri, set when weights are downloaded) are
   *  deliberately absent, so an upsert never resets them. */
  columns: string[];
}

export const ALGORITHM_SPECS_TABLE: CatalogTable = {
  table: "algorithm_specs",
  key: "algorithm_key",
  columns: ["algorithm_key", "label", "applies_to_text", "has_applies_to", "default_applies_to", "applies_to_tooltip", "applies_to_options", "fields"],
};

export const BASE_MODELS_TABLE: CatalogTable = {
  table: "base_models",
  key: "id",
  columns: ["id", "domain", "title", "task", "description", "framework", "license", "uri", "gpu", "compute_profile", "long_description",
            "category", "algorithm_key", "algorithm_variant", "params", "context_window", "alternatives", "default_hp", "supported_hp",
            "sort_order"],
};

export const COMPUTE_POOLS_TABLE: CatalogTable = {
  table: "compute_pools",
  key: "id",
  columns: ["id", "label", "kind", "capacity", "in_use", "queued_jobs"],
};

type AlgorithmSpec = (typeof ALGORITHM_SPECS)[number];
type BaseModel = (typeof ALL_BASE_MODELS)[number];

export function algorithmSpecValues(a: AlgorithmSpec): unknown[] {
  return [a.algorithmKey, a.label, a.appliesToText, a.hasAppliesTo, a.defaultAppliesTo, a.appliesToTooltip,
          a.appliesToOptions ? JSON.stringify(a.appliesToOptions) : null, JSON.stringify(a.fields)];
}

/** `index` carries the curated order through to sort_order — the array's own
 *  sequence IS the intended presentation order. */
export function baseModelValues(b: BaseModel, index: number): unknown[] {
  return [b.id, b.domain, b.title, b.task, b.description, b.framework, b.license, b.uri, b.gpu, b.computeProfile, b.longDescription,
          b.category, b.algorithmKey, b.algorithmVariant, b.params, b.context, b.alternatives,
          b.defaultHp ? JSON.stringify(b.defaultHp) : null,
          b.supportedHp ? JSON.stringify(b.supportedHp) : null,
          index];
}

/**
 * The compute profiles training runs are submitted against. in_use and
 * queued_jobs start at zero: the clusters and dashboard routes compute live
 * occupancy from training_runs/hpo_trials and never read the stored counters,
 * so a real deployment has no reason to start with invented ones. seed.ts
 * overrides them with its demo numbers.
 */
export const COMPUTE_POOLS: { id: string; label: string; kind: "cpu" | "gpu"; capacity: number }[] = [
  { id: "cpu-small", label: "cpu-small", kind: "cpu", capacity: 40 },
  { id: "cpu-large", label: "cpu-large", kind: "cpu", capacity: 20 },
  { id: "gpu-1x", label: "gpu-1x", kind: "gpu", capacity: 12 },
  { id: "gpu-4x", label: "gpu-4x", kind: "gpu", capacity: 4 },
  { id: "gpu-8x", label: "gpu-8x", kind: "gpu", capacity: 2 },
];

export function computePoolValues(p: (typeof COMPUTE_POOLS)[number], counters: { inUse: number; queuedJobs: number } = { inUse: 0, queuedJobs: 0 }): unknown[] {
  return [p.id, p.label, p.kind, p.capacity, counters.inUse, counters.queuedJobs];
}

const placeholders = (n: number) => Array.from({ length: n }, (_, i) => `$${i + 1}`).join(", ");

/** Plain INSERT — seed.ts, into tables it has just dropped and recreated. */
export function insertSql(t: CatalogTable): string {
  return `INSERT INTO ${t.table} (${t.columns.join(", ")}) VALUES (${placeholders(t.columns.length)})`;
}

/**
 * Insert-or-update of every catalog column. The WHERE ... IS DISTINCT FROM
 * guard skips rows that are already current, so a restart with an unchanged
 * catalog writes nothing (no dead tuples, no lock churn) and RETURNING tells
 * inserted (xmax = 0) apart from updated; unchanged rows return no row.
 */
export function upsertSql(t: CatalogTable): string {
  const rest = t.columns.filter((c) => c !== t.key);
  return `${insertSql(t)}
     ON CONFLICT (${t.key}) DO UPDATE SET ${rest.map((c) => `${c} = EXCLUDED.${c}`).join(", ")}
     WHERE (${rest.map((c) => `${t.table}.${c}`).join(", ")}) IS DISTINCT FROM (${rest.map((c) => `EXCLUDED.${c}`).join(", ")})
     RETURNING (xmax = 0) AS inserted`;
}

/** Insert only when the key is new — never touches an existing row. */
export function insertIfMissingSql(t: CatalogTable): string {
  return `${insertSql(t)} ON CONFLICT (${t.key}) DO NOTHING`;
}

/**
 * Boolean env flag, default on. Only an explicit false/0/no/off disables it —
 * a typo'd value keeps the safe default rather than silently turning it off.
 */
export function envFlag(name: string, env: NodeJS.ProcessEnv = process.env): boolean {
  const raw = (env[name] ?? "").trim().toLowerCase();
  return !["false", "0", "no", "off"].includes(raw);
}

/** Session/transaction advisory-lock key. The colon can't appear in a dataset
 *  name, so this never collides with the upload route's hashtext(name) locks. */
export const REFERENCE_DATA_LOCK = "ml-studio:reference-data";

export interface ReferenceDataSummary {
  algorithmSpecs: { inserted: number; updated: number; unchanged: number };
  baseModels: { inserted: number; updated: number; unchanged: number };
  computePools: { inserted: number; existing: number };
}

/**
 * Brings the reference catalog up to date. Idempotent and non-destructive:
 * new rows are inserted, changed catalog columns are updated, nothing is
 * deleted, and compute_pools rows that already exist are left exactly as they
 * are.
 *
 * Several replicas may start at once. The whole load runs in one transaction
 * behind pg_advisory_xact_lock, so replicas take turns: the first does the
 * work, the rest find every row current and write nothing. The upserts would
 * be correct without the lock (ON CONFLICT is race-safe), but two concurrent
 * transactions upserting the same 140 rows would block on each other row by
 * row anyway — the lock makes that one clean wait, and makes the load
 * all-or-nothing: a failure part-way rolls back instead of leaving half a
 * catalog updated.
 */
export async function loadReferenceData(db: ConnectablePool): Promise<ReferenceDataSummary> {
  const client = await db.connect();
  const summary: ReferenceDataSummary = {
    algorithmSpecs: { inserted: 0, updated: 0, unchanged: 0 },
    baseModels: { inserted: 0, updated: 0, unchanged: 0 },
    computePools: { inserted: 0, existing: 0 },
  };
  const tally = (bucket: { inserted: number; updated: number; unchanged: number }, rows: any[]) => {
    if (rows.length === 0) bucket.unchanged++;
    else if (rows[0].inserted) bucket.inserted++;
    else bucket.updated++;
  };
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [REFERENCE_DATA_LOCK]);

    const specSql = upsertSql(ALGORITHM_SPECS_TABLE);
    for (const a of ALGORITHM_SPECS) {
      tally(summary.algorithmSpecs, (await client.query(specSql, algorithmSpecValues(a))).rows);
    }
    const modelSql = upsertSql(BASE_MODELS_TABLE);
    for (const [i, b] of ALL_BASE_MODELS.entries()) {
      tally(summary.baseModels, (await client.query(modelSql, baseModelValues(b, i))).rows);
    }
    const poolSql = insertIfMissingSql(COMPUTE_POOLS_TABLE);
    for (const p of COMPUTE_POOLS) {
      const r = await client.query(poolSql, computePoolValues(p));
      if (r.rowCount) summary.computePools.inserted++;
      else summary.computePools.existing++;
    }

    await client.query("COMMIT");
    return summary;
  } catch (err) {
    await client.query("ROLLBACK").catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}

/**
 * Server-start wrapper: honours SEED_REFERENCE_DATA and never throws. A
 * catalog that couldn't be refreshed is worth a loud warning, not a crash
 * loop — the API still serves whatever catalog the database already has.
 */
export async function loadReferenceDataAtStartup(db: ConnectablePool, env: NodeJS.ProcessEnv = process.env): Promise<ReferenceDataSummary | null> {
  if (!envFlag("SEED_REFERENCE_DATA", env)) {
    // eslint-disable-next-line no-console
    console.log("Reference data: SEED_REFERENCE_DATA=false — not loading algorithm specs, base models or compute pools");
    return null;
  }
  try {
    const s = await loadReferenceData(db);
    // eslint-disable-next-line no-console
    console.log(
      `Reference data loaded — algorithm_specs +${s.algorithmSpecs.inserted} new / ${s.algorithmSpecs.updated} updated / ${s.algorithmSpecs.unchanged} unchanged; ` +
      `base_models +${s.baseModels.inserted} / ${s.baseModels.updated} / ${s.baseModels.unchanged}; ` +
      `compute_pools +${s.computePools.inserted} new / ${s.computePools.existing} already present`
    );
    return s;
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(`WARNING: reference data load failed — continuing with the catalog already in the database: ${err instanceof Error ? err.message : String(err)}`);
    return null;
  }
}
