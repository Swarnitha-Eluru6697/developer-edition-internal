import { describe, expect, it, vi } from "vitest";
import {
  ALGORITHM_SPECS_TABLE, BASE_MODELS_TABLE, COMPUTE_POOLS, COMPUTE_POOLS_TABLE,
  algorithmSpecValues, baseModelValues, envFlag, loadReferenceData, loadReferenceDataAtStartup, upsertSql,
} from "../src/db/referenceData";
import { ALL_BASE_MODELS } from "../src/db/baseModelsData";
import { ALGORITHM_SPECS } from "../src/db/algorithmSpecs";

/**
 * An in-memory stand-in for the three catalog tables that applies the same
 * semantics Postgres gives the loader's SQL: ON CONFLICT DO UPDATE ... WHERE
 * IS DISTINCT FROM (insert / update / no-op, with RETURNING inserted) and ON
 * CONFLICT DO NOTHING. Every statement is recorded, so the tests can assert
 * nothing destructive was ever sent.
 */
function fakeDb(initial: Record<string, Map<string, unknown[]>> = {}) {
  const tables: Record<string, Map<string, unknown[]>> = {
    algorithm_specs: new Map(), base_models: new Map(), compute_pools: new Map(), ...initial,
  };
  const statements: string[] = [];
  let failOn: RegExp | null = null;
  const client = {
    released: 0,
    async query(text: string, values: unknown[] = []) {
      statements.push(text);
      if (failOn && failOn.test(text)) throw new Error("simulated failure");
      const m = text.match(/^INSERT INTO (\w+)/);
      if (!m) return { rows: [], rowCount: 0 };
      const rows = tables[m[1]];
      const key = String(values[0]);
      const prev = rows.get(key);
      if (/DO NOTHING/.test(text)) {
        if (prev) return { rows: [], rowCount: 0 };
        rows.set(key, values);
        return { rows: [], rowCount: 1 };
      }
      if (!prev) { rows.set(key, values); return { rows: [{ inserted: true }], rowCount: 1 }; }
      if (JSON.stringify(prev) === JSON.stringify(values)) return { rows: [], rowCount: 0 };
      rows.set(key, values);
      return { rows: [{ inserted: false }], rowCount: 1 };
    },
    release() { client.released++; },
  };
  return {
    tables, statements, client,
    pool: { connect: vi.fn(async () => client) },
    failWhen(re: RegExp) { failOn = re; },
  };
}

describe("reference data loader", () => {
  it("loads the full catalog on an empty database", async () => {
    const db = fakeDb();
    const s = await loadReferenceData(db.pool);
    expect(s.algorithmSpecs.inserted).toBe(ALGORITHM_SPECS.length);
    expect(s.baseModels.inserted).toBe(ALL_BASE_MODELS.length);
    expect(s.computePools.inserted).toBe(COMPUTE_POOLS.length);
    expect(db.tables.base_models.size).toBe(ALL_BASE_MODELS.length);
    expect(db.client.released).toBe(1);
  });

  it("is idempotent: a second run changes nothing and row counts are stable", async () => {
    const db = fakeDb();
    await loadReferenceData(db.pool);
    const sizes = Object.fromEntries(Object.entries(db.tables).map(([k, v]) => [k, v.size]));
    const again = await loadReferenceData(db.pool);
    expect(again.algorithmSpecs).toEqual({ inserted: 0, updated: 0, unchanged: ALGORITHM_SPECS.length });
    expect(again.baseModels).toEqual({ inserted: 0, updated: 0, unchanged: ALL_BASE_MODELS.length });
    expect(again.computePools).toEqual({ inserted: 0, existing: COMPUTE_POOLS.length });
    expect(Object.fromEntries(Object.entries(db.tables).map(([k, v]) => [k, v.size]))).toEqual(sizes);
  });

  it("never deletes: rows outside the catalog survive, changed catalog rows are updated", async () => {
    const custom = ["custom-model", "ml", "custom", "Classification"];
    const stale = baseModelValues(ALL_BASE_MODELS[0], 0);
    stale[4] = "an older description";
    const db = fakeDb({ base_models: new Map([["custom-model", custom], [String(stale[0]), stale]]) });
    const s = await loadReferenceData(db.pool);
    expect(db.tables.base_models.get("custom-model")).toEqual(custom);
    expect(db.tables.base_models.get(String(stale[0]))![4]).toBe(ALL_BASE_MODELS[0].description);
    expect(s.baseModels.updated).toBe(1);
    expect(db.tables.base_models.size).toBe(ALL_BASE_MODELS.length + 1);
  });

  it("leaves an existing compute pool exactly as it is (counters, capacity)", async () => {
    const live = ["gpu-1x", "gpu-1x", "gpu", 99, 7, 3];
    const db = fakeDb({ compute_pools: new Map([["gpu-1x", live]]) });
    const s = await loadReferenceData(db.pool);
    expect(db.tables.compute_pools.get("gpu-1x")).toEqual(live);
    expect(s.computePools).toEqual({ inserted: COMPUTE_POOLS.length - 1, existing: 1 });
  });

  it("sends no destructive SQL, and runs in one transaction behind an advisory lock", async () => {
    const db = fakeDb();
    await loadReferenceData(db.pool);
    expect(db.statements.some((q) => /\b(DROP|DELETE|TRUNCATE)\b/i.test(q))).toBe(false);
    expect(db.statements[0]).toBe("BEGIN");
    expect(db.statements[1]).toMatch(/pg_advisory_xact_lock/);
    expect(db.statements[db.statements.length - 1]).toBe("COMMIT");
    expect(db.statements.filter((q) => q.startsWith("INSERT INTO compute_pools")).every((q) => /ON CONFLICT \(id\) DO NOTHING$/.test(q))).toBe(true);
  });

  it("rolls back and releases the connection on failure", async () => {
    const db = fakeDb();
    db.failWhen(/^INSERT INTO base_models/);
    await expect(loadReferenceData(db.pool)).rejects.toThrow("simulated failure");
    expect(db.statements).toContain("ROLLBACK");
    expect(db.statements).not.toContain("COMMIT");
    expect(db.client.released).toBe(1);
  });

  it("startup wrapper never throws and logs a warning instead", async () => {
    const db = fakeDb();
    db.failWhen(/^INSERT INTO algorithm_specs/);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    await expect(loadReferenceDataAtStartup(db.pool, {})).resolves.toBeNull();
    expect(warn.mock.calls[0][0]).toMatch(/reference data load failed/);
    warn.mockRestore();
  });

  it("startup wrapper honours SEED_REFERENCE_DATA=false", async () => {
    const db = fakeDb();
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    expect(await loadReferenceDataAtStartup(db.pool, { SEED_REFERENCE_DATA: "false" })).toBeNull();
    expect(db.pool.connect).not.toHaveBeenCalled();
    log.mockRestore();
  });
});

describe("reference data SQL and values", () => {
  it("upserts every catalog column except the key, and none of the runtime columns", () => {
    const sql = upsertSql(BASE_MODELS_TABLE);
    expect(sql).toMatch(/ON CONFLICT \(id\) DO UPDATE SET domain = EXCLUDED\.domain/);
    expect(sql).not.toMatch(/SET id =|stored_weights_uri|weights_downloaded_at/);
    expect(sql).toMatch(/IS DISTINCT FROM/);
  });

  it("produces one value per column, with sort_order = array index", () => {
    expect(algorithmSpecValues(ALGORITHM_SPECS[0])).toHaveLength(ALGORITHM_SPECS_TABLE.columns.length);
    const last = ALL_BASE_MODELS.length - 1;
    const v = baseModelValues(ALL_BASE_MODELS[last], last);
    expect(v).toHaveLength(BASE_MODELS_TABLE.columns.length);
    expect(v[BASE_MODELS_TABLE.columns.indexOf("sort_order")]).toBe(last);
    expect(COMPUTE_POOLS_TABLE.columns).toHaveLength(6);
  });

  it("envFlag defaults on and only an explicit false-ish value disables", () => {
    expect(envFlag("X", {})).toBe(true);
    expect(envFlag("X", { X: "true" })).toBe(true);
    expect(envFlag("X", { X: "flase" })).toBe(true);
    for (const off of ["false", "FALSE", "0", "no", "off", " false "]) expect(envFlag("X", { X: off })).toBe(false);
  });
});
