import { beforeEach, describe, expect, it, vi } from "vitest";
import { createHash } from "crypto";

// The loader's collaborators: Postgres, object storage and the upload path.
// Mocked so the skip/never-touch-user-data rules can be checked without a
// live database — the real end-to-end run is documented in the CHANGELOG.
const db = vi.hoisted(() => ({
  query: vi.fn(),
  lockQuery: vi.fn(),
}));
vi.mock("../src/db/connection", () => ({
  pool: {
    query: db.query,
    connect: vi.fn(async () => ({ query: db.lockQuery, release: vi.fn() })),
  },
  initSchema: vi.fn(),
}));
const storage = vi.hoisted(() => ({ getStorage: vi.fn() }));
vi.mock("../src/storage", () => storage);
const upload = vi.hoisted(() => ({ ingestDatasetFile: vi.fn(), insertDatasetRecord: vi.fn() }));
vi.mock("../src/routes/datasets", () => upload);

import {
  SAMPLE_DATASETS, SAMPLE_DATASET_OWNER, dailySalesCsv, fraudTransactionsCsv, housePricesCsv,
  loadSampleDatasets, loadSampleDatasetsAtStartup, sampleMarkerKey,
} from "../src/db/sampleDatasets";

const parse = (csv: string) => {
  const [header, ...lines] = csv.trim().split("\n");
  return { header: header.split(","), rows: lines.map((l) => l.split(",")) };
};
const sha = (s: string) => createHash("sha256").update(s).digest("hex");

describe("sample dataset generators", () => {
  it("are deterministic — identical bytes on every call", () => {
    for (const gen of [fraudTransactionsCsv, housePricesCsv, dailySalesCsv]) expect(sha(gen())).toBe(sha(gen()));
  });

  it("fraud: binary is_fraudulent target with a plausible minority rate", () => {
    const { header, rows } = parse(fraudTransactionsCsv());
    expect(rows).toHaveLength(4000);
    const t = header.indexOf("is_fraudulent");
    expect(t).toBe(header.length - 1);
    expect(new Set(rows.map((r) => r[t]))).toEqual(new Set(["0", "1"]));
    const rate = rows.filter((r) => r[t] === "1").length / rows.length;
    expect(rate).toBeGreaterThan(0.03);
    expect(rate).toBeLessThan(0.15);
  });

  it("house prices: numeric price that rises with living area", () => {
    const { header, rows } = parse(housePricesCsv());
    expect(rows).toHaveLength(2500);
    const sqft = rows.map((r) => Number(r[header.indexOf("sqft_living")]));
    const price = rows.map((r) => Number(r[header.indexOf("price")]));
    expect(price.every((p) => Number.isFinite(p) && p > 0)).toBe(true);
    const mean = (a: number[]) => a.reduce((x, y) => x + y, 0) / a.length;
    const [ms, mp] = [mean(sqft), mean(price)];
    const cov = mean(sqft.map((s, i) => (s - ms) * (price[i] - mp)));
    const corr = cov / Math.sqrt(mean(sqft.map((s) => (s - ms) ** 2)) * mean(price.map((p) => (p - mp) ** 2)));
    expect(corr).toBeGreaterThan(0.6);
  });

  it("daily sales: two years of consecutive daily dates, no gaps", () => {
    const { header, rows } = parse(dailySalesCsv());
    expect(header).toEqual(["date", "sales"]);
    expect(rows).toHaveLength(731);
    expect(rows[0][0]).toBe("2024-01-01");
    expect(rows[rows.length - 1][0]).toBe("2025-12-31");
    for (let i = 1; i < rows.length; i++) {
      expect(Date.parse(rows[i][0]) - Date.parse(rows[i - 1][0])).toBe(86_400_000);
    }
  });
});

describe("loadSampleDatasets", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    storage.getStorage.mockReturnValue({ backend: "s3" });
    db.lockQuery.mockImplementation(async (q: string) => ({ rows: [{ locked: /try_advisory_lock/.test(q) }] }));
    upload.ingestDatasetFile.mockResolvedValue({ status: 200, body: { snapshotVersion: 1, rows: 10, storageUri: "s3://b/k" } });
  });
  /** db.query answering from a tiny state: markers, datasets (name → owner), snapshot names. */
  const state = (s: { markers?: string[]; datasets?: Record<string, string>; snapshots?: string[] }) => {
    db.query.mockImplementation(async (q: string, v: unknown[] = []) => {
      if (q.startsWith("SELECT 1 FROM seed_markers")) return { rows: (s.markers ?? []).includes(String(v[0])) ? [{}] : [] };
      if (q.startsWith("SELECT owner FROM datasets")) { const o = s.datasets?.[String(v[0])]; return { rows: o ? [{ owner: o }] : [] }; }
      if (q.startsWith("SELECT 1 FROM dataset_snapshots")) return { rows: (s.snapshots ?? []).includes(String(v[0])) ? [{}] : [] };
      return { rows: [], rowCount: 1 };
    });
  };
  const markerWrites = () => db.query.mock.calls.filter(([q]) => String(q).startsWith("INSERT INTO seed_markers")).map(([, v]) => v[0]);
  const log = () => { vi.spyOn(console, "log").mockImplementation(() => {}); vi.spyOn(console, "warn").mockImplementation(() => {}); };

  it("loads every sample on a fresh database through the upload path, then marks it", async () => {
    log(); state({});
    const out = await loadSampleDatasets();
    expect(Object.values(out!)).toEqual(["loaded", "loaded", "loaded"]);
    expect(upload.insertDatasetRecord).toHaveBeenCalledTimes(3);
    expect(upload.ingestDatasetFile).toHaveBeenCalledTimes(3);
    const call = upload.ingestDatasetFile.mock.calls[0][0];
    expect(call).toMatchObject({ datasetName: SAMPLE_DATASETS[0].name, format: "csv", actor: SAMPLE_DATASET_OWNER });
    expect(markerWrites()).toEqual(SAMPLE_DATASETS.map((d) => sampleMarkerKey(d.name)));
  });

  it("skips a sample that was loaded before — even if it has since been deleted", async () => {
    log(); state({ markers: SAMPLE_DATASETS.map((d) => sampleMarkerKey(d.name)) });
    const out = await loadSampleDatasets();
    expect(Object.values(out!)).toEqual(["previously-loaded", "previously-loaded", "previously-loaded"]);
    expect(upload.ingestDatasetFile).not.toHaveBeenCalled();
    expect(upload.insertDatasetRecord).not.toHaveBeenCalled();
  });

  it("never touches a user's dataset that shares a sample's name", async () => {
    log(); state({ datasets: { "sample-house-prices": "alice" } });
    const out = await loadSampleDatasets();
    expect(out!["sample-house-prices"]).toBe("name-taken");
    expect(upload.ingestDatasetFile.mock.calls.map(([o]) => o.datasetName)).not.toContain("sample-house-prices");
  });

  it("does not re-upload a sample that already has a snapshot", async () => {
    log(); state({ datasets: { "sample-daily-sales": SAMPLE_DATASET_OWNER }, snapshots: ["sample-daily-sales"] });
    const out = await loadSampleDatasets();
    expect(out!["sample-daily-sales"]).toBe("already-loaded");
    expect(upload.ingestDatasetFile.mock.calls.map(([o]) => o.datasetName)).not.toContain("sample-daily-sales");
  });

  it("leaves no marker when the upload fails, so the next start retries", async () => {
    log(); state({});
    upload.ingestDatasetFile.mockResolvedValue({ status: 503, body: { error: "storage down" } });
    const out = await loadSampleDatasets();
    expect(Object.values(out!)).toEqual(["failed", "failed", "failed"]);
    expect(markerWrites()).toEqual([]);
  });

  it("degrades with a warning when STORAGE_BACKEND is not configured", async () => {
    log(); state({});
    storage.getStorage.mockReturnValue(null);
    expect(await loadSampleDatasets()).toBeNull();
    expect(vi.mocked(console.warn).mock.calls[0][0]).toMatch(/STORAGE_BACKEND is not configured/);
    expect(db.query).not.toHaveBeenCalled();
  });

  it("skips when another replica holds the lock", async () => {
    log(); state({});
    db.lockQuery.mockResolvedValue({ rows: [{ locked: false }] });
    expect(await loadSampleDatasets()).toBeNull();
    expect(db.query).not.toHaveBeenCalled();
  });

  it("startup wrapper honours SEED_SAMPLE_DATASETS=false and never throws", async () => {
    log(); state({});
    await loadSampleDatasetsAtStartup({ SEED_SAMPLE_DATASETS: "false" });
    expect(storage.getStorage).not.toHaveBeenCalled();
    db.lockQuery.mockRejectedValue(new Error("db down"));
    await expect(loadSampleDatasetsAtStartup({})).resolves.toBeUndefined();
    expect(vi.mocked(console.warn).mock.calls.at(-1)![0]).toMatch(/sample dataset load failed/);
  });

  it("one sample failing doesn't stop the others", async () => {
    log(); state({});
    upload.insertDatasetRecord.mockRejectedValueOnce(new Error("db hiccup"));
    const out = await loadSampleDatasets();
    expect(Object.values(out!)).toEqual(["failed", "loaded", "loaded"]);
  });
});
